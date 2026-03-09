const ROOM = decodeURIComponent(window.location.pathname.split('/').pop() || 'default');
const READ_ONLY = Boolean(window.SCOREBOARD_READ_ONLY);

const DEFAULT_STATE = {
  roomName: ROOM,
  redName: 'Esquina Roja',
  blueName: 'Esquina Azul',
  redScore: 0,
  blueScore: 0,
  redPenalties: 0,
  bluePenalties: 0,
  timeLeft: 120,
  roundDuration: 120,
  restDuration: 30,
  running: false,
  phase: 'idle',
  matchStage: 'SEMIFINAL',
  currentRound: 1,
  redRoundsWon: 0,
  blueRoundsWon: 0,
  roundWinner: null,
  matchWinner: null,
  maxRounds: 3,
  history: [],
  resultRecorded: false,
  revision: 0
};

let state = { ...DEFAULT_STATE };
let timerInterval = null;
let syncInterval = null;
let presenceInterval = null;

let isSaving = false;
let localMutationId = 0;
let syncedMutationId = 0;

let actionQueue = [];
let processingActions = false;
let authFailed = false;

function normalizeState(partial) {
  return { ...DEFAULT_STATE, ...(partial || {}) };
}

function handleUnauthorized() {
  if (authFailed) return;
  authFailed = true;

  clearInterval(syncInterval);
  clearInterval(timerInterval);
  syncInterval = null;
  timerInterval = null;

  // Evita spam de 401 en consola y lleva al login.
  window.location.href = '/login';
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function getMaxRounds() {
  const maxRounds = Number(state.maxRounds);
  return Number.isFinite(maxRounds) && maxRounds > 0 ? Math.floor(maxRounds) : 3;
}

function getRevision(s) {
  return Number.isFinite(s?.revision) ? s.revision : 0;
}

function notifyStateChange() {
  if (typeof onStateChange === 'function') {
    onStateChange(state);
  }
}

function setState(newState, persist = false) {
  const next = normalizeState(newState);
  const currentRevision = getRevision(state);
  const nextRevision = getRevision(next);

  if (nextRevision < currentRevision) return;

  // Evita "rebote" visual del reloj por polling con estado viejo:
  // si estamos corriendo (o en descanso), no aceptar un tiempo mayor
  // dentro del mismo round/phase/revision.
  if (
    !READ_ONLY &&
    (state.phase === 'running' || state.phase === 'rest') &&
    state.phase === next.phase &&
    state.currentRound === next.currentRound &&
    nextRevision <= currentRevision &&
    Number(next.timeLeft) > Number(state.timeLeft)
  ) {
    return;
  }

  state = next;
  notifyStateChange();

  if (persist && !READ_ONLY) {
    markDirtyAndSave();
  }
}

async function fetchState() {
  const res = await fetch('/state/' + encodeURIComponent(ROOM));
  if (res.status === 401) {
    handleUnauthorized();
    throw new Error('unauthorized');
  }
  if (!res.ok) throw new Error('No se pudo cargar el estado');
  return normalizeState(await res.json());
}

async function saveLoop() {
  if (READ_ONLY || isSaving) return;
  isSaving = true;

  try {
    while (syncedMutationId < localMutationId) {
      const targetMutationId = localMutationId;

      const res = await fetch('/update/' + encodeURIComponent(ROOM), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(state)
      });

      if (res.status === 409) {
        const payload = await res.json();
        if (payload?.state) {
          state = normalizeState(payload.state);
          notifyStateChange();
        }
        continue;
      }

      if (res.status === 401) {
        handleUnauthorized();
        break;
      }

      if (!res.ok) break;
      const payload = await res.json();
      if (payload?.state) {
        state = normalizeState(payload.state);
        notifyStateChange();
      }

      syncedMutationId = Math.max(syncedMutationId, targetMutationId);
    }
  } catch (_err) {
    // reintento automático en el próximo tick
  } finally {
    isSaving = false;
  }
}

function markDirtyAndSave() {
  if (READ_ONLY) return;
  localMutationId++;
  void saveLoop();
}

async function runAction(action) {
  if (READ_ONLY || authFailed) return false;

  const res = await fetch('/action/' + encodeURIComponent(ROOM), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(action)
  });

  if (res.status === 401) {
    handleUnauthorized();
    return false;
  }

  if (!res.ok) return false;

  const updated = normalizeState(await res.json());
  setState(updated, false);
  return true;
}

function enqueueAction(action) {
  if (READ_ONLY) return;

  actionQueue.push(action);
  void processActionQueue();
}

async function processActionQueue() {
  if (processingActions) return;
  processingActions = true;

  try {
    while (actionQueue.length > 0) {
      const action = actionQueue[0];
      const ok = await runAction(action);

      if (ok) {
        actionQueue.shift();
        continue;
      }

      await wait(120);
    }
  } finally {
    processingActions = false;
  }
}

async function refreshFromServer() {
  if (authFailed) return;
  if (READ_ONLY && document.hidden) return;
  try {
    const remote = await fetchState();
    if (syncedMutationId < localMutationId) return;
    setState(remote, false);
  } catch (_err) {
    // keep polling
  }
}

function ensureSyncPolling() {
  if (syncInterval) return;
  const intervalMs = READ_ONLY ? 1800 : 700;
  syncInterval = setInterval(refreshFromServer, intervalMs);
}

async function sendPresenceHeartbeat() {
  if (!READ_ONLY || authFailed) return;

  try {
    const res = await fetch('/presence/' + encodeURIComponent(ROOM), {
      method: 'POST'
    });
    if (res.status === 401) {
      handleUnauthorized();
    }
  } catch (_err) {
    // heartbeat best-effort
  }
}

function ensurePresenceHeartbeat() {
  if (!READ_ONLY || presenceInterval) return;
  void sendPresenceHeartbeat();
  presenceInterval = setInterval(sendPresenceHeartbeat, 10000);
}

function startTimer() {
  if (READ_ONLY || state.running || authFailed || state.phase === 'rest') return;

  state.running = true;
  state.phase = 'running';
  notifyStateChange();
  markDirtyAndSave();

  timerInterval = setInterval(() => {
    if (state.timeLeft > 0) {
      state.timeLeft--;
      notifyStateChange();
      markDirtyAndSave();
      return;
    }

    endRound();
  }, 1000);
}

function pauseTimer() {
  if (READ_ONLY || (!state.running && !timerInterval)) return;

  state.running = false;
  state.phase = 'paused';

  clearInterval(timerInterval);
  timerInterval = null;

  notifyStateChange();
  markDirtyAndSave();
}

function endRound() {
  if (READ_ONLY) return;

  clearInterval(timerInterval);
  timerInterval = null;

  state.running = false;

  if (state.redScore > state.blueScore) {
    state.redRoundsWon++;
    state.roundWinner = 'red';
  } else if (state.blueScore > state.redScore) {
    state.blueRoundsWon++;
    state.roundWinner = 'blue';
  } else {
    state.roundWinner = null;
  }

  const maxRounds = getMaxRounds();

  if (
    state.redRoundsWon >= Math.ceil(maxRounds / 2) ||
    state.blueRoundsWon >= Math.ceil(maxRounds / 2) ||
    state.currentRound >= maxRounds
  ) {
    state.phase = 'match_end';
    if (state.redRoundsWon > state.blueRoundsWon) {
      state.matchWinner = 'red';
    } else if (state.blueRoundsWon > state.redRoundsWon) {
      state.matchWinner = 'blue';
    } else if (state.redScore > state.blueScore) {
      state.matchWinner = 'red';
    } else if (state.blueScore > state.redScore) {
      state.matchWinner = 'blue';
    } else {
      state.matchWinner = null;
    }
  } else {
    state.phase = 'rest';
    state.timeLeft = Number.isFinite(state.restDuration) ? state.restDuration : 30;
    startRestCountdown();
  }

  notifyStateChange();
  markDirtyAndSave();
}

function startRestCountdown() {
  if (READ_ONLY || authFailed) return;

  clearInterval(timerInterval);
  timerInterval = setInterval(() => {
    if (state.timeLeft > 0) {
      state.timeLeft--;
      notifyStateChange();
      markDirtyAndSave();
      return;
    }

    clearInterval(timerInterval);
    timerInterval = null;

    if (state.phase === 'rest') {
      advanceRound();
    }
  }, 1000);
}

function advanceRound() {
  if (READ_ONLY) return;
  if (state.currentRound >= getMaxRounds()) return;
  if (state.phase === 'match_end') return;

  state.currentRound++;
  state.redScore = 0;
  state.blueScore = 0;
  state.redPenalties = 0;
  state.bluePenalties = 0;
  state.timeLeft = Number.isFinite(state.roundDuration) ? state.roundDuration : 120;
  state.phase = 'idle';
  state.roundWinner = null;

  notifyStateChange();
  markDirtyAndSave();
}

function resetMatch() {
  if (READ_ONLY) return;

  clearInterval(timerInterval);
  timerInterval = null;

  state = normalizeState({ ...DEFAULT_STATE, roomName: ROOM, revision: state.revision });

  notifyStateChange();
  markDirtyAndSave();
}

function addScore(side, pts) {
  enqueueAction({ type: 'add_score', side, pts });
}

function addPenalty(side) {
  enqueueAction({ type: 'add_penalty', side });
}

function setCompetitorNames(redName, blueName) {
  enqueueAction({ type: 'set_names', redName, blueName });
}

function setRestDuration(seconds) {
  enqueueAction({ type: 'set_rest_duration', seconds });
}

function formatTime(secs) {
  const safeSecs = Number.isFinite(secs) ? Math.max(0, Math.floor(secs)) : 0;
  const m = Math.floor(safeSecs / 60);
  const s = safeSecs % 60;
  return m + ':' + String(s).padStart(2, '0');
}

function onStateChange(_s) {}

(async function initStateSync() {
  try {
    const initialState = await fetchState();
    setState(initialState, false);
    ensureSyncPolling();
    ensurePresenceHeartbeat();
  } catch (_err) {
    // 401 ya manejado en fetchState
  }
})();
