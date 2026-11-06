/** Open a permitted group without accidentally folding an already open menu. */
export async function expandNavigation(page, label, root = page.locator(".sidebar")) {
  const button = root.getByRole("button", { name: label, exact: true });
  if (await button.getAttribute("aria-expanded") !== "true") await button.click();
}
