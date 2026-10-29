import { ApiError, authHeaders, detailMessage, markConnection } from './api';

export async function downloadPaper(id: string, signal?: AbortSignal) {
  const response = await fetch(`/api/papers/${encodeURIComponent(id)}/file`, { headers: authHeaders(), signal });
  markConnection('online');
  if (!response.ok) throw new ApiError(response.status, detailMessage(await response.json().catch(() => null)));
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = `${id}.pdf`;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
