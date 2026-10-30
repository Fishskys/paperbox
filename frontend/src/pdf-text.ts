export function escapeHtml(text: string) {
  return text.replace(/[&<>"']/g, (value) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[value]!);
}

export function highlightText(text: string, query: string) {
  const term = query.trim().toLowerCase();
  if (!term) return escapeHtml(text);
  const source = text.toLowerCase();
  let start = 0, position = source.indexOf(term), result = "";
  while (position >= 0) {
    result += escapeHtml(text.slice(start, position)) + "<mark>" + escapeHtml(text.slice(position, position + term.length)) + "</mark>";
    start = position + term.length; position = source.indexOf(term, start);
  }
  return result + escapeHtml(text.slice(start));
}

export function findText(text: string, query: string) {
  const term = query.trim().replace(/\s+/g, " ").toLowerCase();
  const normalized = text.replace(/\s+/g, " ");
  if (!term) return null;
  const source = normalized.toLowerCase();
  let position = source.indexOf(term), count = 0;
  const first = position;
  while (position >= 0) { count++; position = source.indexOf(term, position + term.length); }
  return count ? { count, snippet: normalized.slice(Math.max(0, first - 45), first + term.length + 90) } : null;
}