export const createViews = ({ element, loadActivity, loadMarkets, loadReports }) => {
  const views = ['leaderboard', 'activity', 'markets', 'reports'];
  const navigationIds = { leaderboard: 'lb', activity: 'act', markets: 'markets', reports: 'reports' };

  const show = (view) => {
    if (!views.includes(view)) return;
    for (const name of views) {
      element(`view-${name}`).hidden = name !== view;
      element(`nav-${navigationIds[name]}`).classList.toggle('active', name === view);
    }
    if (view === 'activity') return loadActivity();
    if (view === 'markets') return loadMarkets();
    if (view === 'reports') return loadReports();
  };

  return { show };
};
