/** Keep owner navigation on the app origin when a valid backend is configured. */
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
    return "/manage";
  } catch {
    return null;
  }
}
