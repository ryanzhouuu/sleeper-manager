/** Link to the owner editor on the same Worker that supplies the public document. */
export function managerControlsUrl(homeUrl: string | undefined): string | null {
  if (!homeUrl) return null;
  try {
    const url = new URL(homeUrl);
    if (
      !["https:", "http:"].includes(url.protocol) ||
      url.username ||
      url.password
    ) {
      return null;
    }
    return new URL("/manage", url.origin).href;
  } catch {
    return null;
  }
}
