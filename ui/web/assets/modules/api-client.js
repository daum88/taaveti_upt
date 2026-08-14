export class ApiRequestError extends Error {
  constructor(status, data) {
    super(
      typeof data?.error === 'string'
        ? data.error
        : typeof data?.detail === 'string'
          ? data.detail
          : `Request failed (HTTP ${status}). Please try again.`,
    );
    this.name = 'ApiRequestError';
    this.status = status;
    this.data = data;
  }
}

const decodeJson = async (response) => {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    throw new ApiRequestError(response.status, null);
  }
};

/**
 * Sends a JSON request and returns its decoded successful response.
 *
 * @param {string} path
 * @param {{method?: string, body?: unknown, headers?: HeadersInit, signal?: AbortSignal}} options
 * @returns {Promise<unknown>}
 * @throws {ApiRequestError} when the server returns a non-success response or invalid JSON
 */
export const requestJson = async (path, { method = 'GET', body, headers, signal } = {}) => {
  const hasBody = body !== undefined;
  const response = await fetch(path, {
    method,
    headers: hasBody ? { 'Content-Type': 'application/json', ...headers } : headers,
    body: hasBody ? JSON.stringify(body) : undefined,
    signal,
  });
  const data = await decodeJson(response);
  if (!response.ok) throw new ApiRequestError(response.status, data);
  return data;
};
