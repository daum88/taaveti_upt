export const registerChartZoom = () => {
  if (window.ChartZoom) Chart.register(ChartZoom);
};

export const destroyChart = (target) => {
  const existing = Chart.getChart(target);
  if (existing) existing.destroy();
};

export const replaceChart = (canvas, config) => {
  destroyChart(canvas);
  return new Chart(canvas, config);
};
