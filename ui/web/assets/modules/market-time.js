/**
 * Market-time axis: maps real timestamps to a compressed coordinate where
 * market-closed intervals (weekends, holidays) occupy zero width, and back.
 *
 * Intervals are `{ start, end }` epoch milliseconds, half-open. Inside a closed
 * interval `compress` is constant (the seam point), so `decompress` of a seam
 * returns the reopen instant. Both functions are monotonic.
 */
export const createMarketTimeAxis = (intervals) => {
  const sorted = (Array.isArray(intervals) ? intervals : [])
    .map((interval) => ({ start: Number(interval?.start), end: Number(interval?.end) }))
    .filter((interval) => Number.isFinite(interval.start) && Number.isFinite(interval.end) && interval.end > interval.start)
    .sort((left, right) => left.start - right.start);

  const spans = [];
  let removed = 0;
  for (const { start, end } of sorted) {
    const last = spans.at(-1);
    if (last && start <= last.end) {
      if (end > last.end) {
        removed += end - last.end;
        last.end = end;
        last.removedAfter = removed;
      }
      continue;
    }
    const span = { start, end, seam: start - removed };
    removed += end - start;
    span.removedAfter = removed;
    spans.push(span);
  }

  const rightmostIndex = (keyOf, value) => {
    let low = 0;
    let high = spans.length - 1;
    let result = -1;
    while (low <= high) {
      const mid = (low + high) >> 1;
      if (keyOf(spans[mid]) <= value) {
        result = mid;
        low = mid + 1;
      } else {
        high = mid - 1;
      }
    }
    return result;
  };

  const compress = (time) => {
    const index = rightmostIndex((span) => span.start, time);
    if (index === -1) return time;
    const span = spans[index];
    return time < span.end ? span.seam : time - span.removedAfter;
  };

  const decompress = (value) => {
    const index = rightmostIndex((span) => span.seam, value);
    if (index === -1) return value;
    const span = spans[index];
    return value === span.seam ? span.end : value + span.removedAfter;
  };

  const isClosed = (time) => {
    const index = rightmostIndex((span) => span.start, time);
    return index !== -1 && time < spans[index].end;
  };

  return { compress, decompress, isClosed };
};
