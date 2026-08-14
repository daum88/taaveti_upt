export function createRefreshCoordinator({ requestJson, leaderboard, getCurrentDetail, renderDetail }) {
  let inFlight = null;
  let pending = false;

  async function refreshDrawerDetail() {
    const currentDetail = getCurrentDetail();
    if (!currentDetail) return;
    const detail = await requestJson(`/api/agent-detail/${currentDetail.username}`);
    if (!detail.error && getCurrentDetail()?.username === detail.username) {
      renderDetail(detail);
    }
  }

  function refresh() {
    if (inFlight) {
      pending = true;
      return inFlight;
    }
    inFlight = (async () => {
      do {
        pending = false;
        await leaderboard.load();
        await refreshDrawerDetail();
      } while (pending);
    })();
    const settled = inFlight.finally(() => { inFlight = null; });
    return settled;
  }

  return {
    refresh,
    get inFlight() { return inFlight; },
  };
}
