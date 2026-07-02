const trustedImageHosts = new Set(["images.unsplash.com"]);
const trustedSameOriginImagePrefixes = ["/assets/", "/sample-data/"];
const trustedImageExtensions = new Set([
  ".avif",
  ".gif",
  ".jpeg",
  ".jpg",
  ".png",
  ".svg",
  ".webp",
]);

export function trustedImageUrl(
  value,
  baseUrl,
  trustedHosts = trustedImageHosts,
) {
  if (typeof value !== "string" || !value.trim()) return null;
  try {
    const url = new URL(value, baseUrl);
    const base = new URL(baseUrl);
    if (url.origin === base.origin && isSameOriginImagePath(url)) return url.href;
    if (url.protocol === "https:" && trustedHosts.has(url.hostname)) {
      return url.href;
    }
  } catch {
    return null;
  }
  return null;
}

function isSameOriginImagePath(url) {
  if (
    !trustedSameOriginImagePrefixes.some((prefix) =>
      url.pathname.startsWith(prefix),
    )
  ) {
    return false;
  }
  return trustedImageExtensions.has(extensionForPath(url.pathname));
}

function extensionForPath(pathname) {
  const lastDot = pathname.lastIndexOf(".");
  return lastDot === -1 ? "" : pathname.slice(lastDot).toLowerCase();
}
