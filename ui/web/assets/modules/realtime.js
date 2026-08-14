export const createRealtimeRouter = ({ isViewVisible, actions }) => {
  const isExecutedTradeUpdate = (message) => message.type === 'GATEKEEPER_ALERT' && message.status === 'EXECUTED';

  const handleMessage = (message) => {
    if (message.type === 'DECISION_BATCH_UPDATED') actions.renderDecisionBatchStatus(message.data);

    const affectsLeaderboard = message.type === 'LEADERBOARD_UPDATE';
    const affectsActivity =
      message.type === 'TRANSACTION_UPDATE' || message.type === 'PORTFOLIO_RESET' || isExecutedTradeUpdate(message);

    if (affectsActivity && isViewVisible('activity')) actions.loadActivity();
    if (affectsLeaderboard && isViewVisible('leaderboard')) actions.refreshLeaderboard();
  };

  return { handleMessage };
};

export const startRealtime = ({ onMessage, onResume, reconnectDelay = 5_000 }) => {
  let reconnectTimer;
  let stopped = false;
  let socket;

  const connect = () => {
    if (stopped) return;
    try {
      const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
      socket = new WebSocket(`${protocol}://${location.host}/ws`);
      socket.onmessage = (event) => {
        try {
          onMessage(JSON.parse(event.data));
        } catch (_) {}
      };
      socket.onclose = () => {
        if (!stopped) reconnectTimer = setTimeout(connect, reconnectDelay);
      };
    } catch (_) {
      reconnectTimer = setTimeout(connect, reconnectDelay);
    }
  };

  const resume = () => {
    if (!stopped) onResume();
  };
  const onVisibilityChange = () => {
    if (document.visibilityState === 'visible') resume();
  };

  document.addEventListener('visibilitychange', onVisibilityChange);
  window.addEventListener('focus', resume);
  connect();

  return () => {
    stopped = true;
    clearTimeout(reconnectTimer);
    socket?.close();
    document.removeEventListener('visibilitychange', onVisibilityChange);
    window.removeEventListener('focus', resume);
  };
};
