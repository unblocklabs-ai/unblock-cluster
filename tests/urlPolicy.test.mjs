import assert from "node:assert/strict";
import test from "node:test";

import { trustedImageUrl } from "../src/urlPolicy.js";

const baseUrl = "https://viewer.example/clusters/dg_abc";

test("allows same-origin image paths", () => {
  assert.equal(
    trustedImageUrl("/sample-data/image.jpg", baseUrl),
    "https://viewer.example/sample-data/image.jpg",
  );
  assert.equal(
    trustedImageUrl("/assets/rendered/image.webp?size=sm", baseUrl),
    "https://viewer.example/assets/rendered/image.webp?size=sm",
  );
});

test("allows trusted HTTPS image hosts", () => {
  const url = "https://images.unsplash.com/photo.jpg?auto=format";

  assert.equal(trustedImageUrl(url, baseUrl), url);
});

test("blocks untrusted external image hosts", () => {
  assert.equal(trustedImageUrl("https://attacker.example/pixel", baseUrl), null);
});

test("blocks same-origin API and non-image paths", () => {
  assert.equal(
    trustedImageUrl("/api/graphs/grf_abc/views/view_abc/artifact", baseUrl),
    null,
  );
  assert.equal(trustedImageUrl("/clusters/dg_abc", baseUrl), null);
  assert.equal(trustedImageUrl("/sample-data/restaurants.json", baseUrl), null);
});

test("blocks non-string and invalid image values", () => {
  assert.equal(trustedImageUrl(true, baseUrl), null);
  assert.equal(trustedImageUrl({ url: "/image.jpg" }, baseUrl), null);
  assert.equal(trustedImageUrl("https://", baseUrl), null);
});
