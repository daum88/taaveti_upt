export const createViews = ({ element, loadActivity, loadMarkets }) => {
  const views = ['leaderboard', 'activity', 'markets'];
  const navigationIds = { leaderboard: 'lb', activity: 'act', markets: 'markets' };

  const show = (view) => {
    if (!views.includes(view)) return;
    for (const name of views) {
      element(`view-${name}`).hidden = name !== view;
      element(`nav-${navigationIds[name]}`).classList.toggle('active', name === view);
    }
    if (view === 'activity') return loadActivity();
    if (view === 'markets') return loadMarkets();
  };

  return { show };
};
