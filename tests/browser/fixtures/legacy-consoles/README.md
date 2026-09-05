# Legacy console presentation fixtures

Archive and VPS Health are already installed on production independently of the
source-built platform apps. These fixtures let the isolated preview job verify
their shared appearance without importing their backends or using production data.

The CSS and JavaScript are byte-for-byte copies from the owned release wheels
identified by `provenance.json`. HTML was rendered from those same verified
templates using invented records and all director controls enabled. URL tags point
to intercepted `/mock/` endpoints; POST requests are rejected by the test.
The shared theme itself is read from the source-built test site's actual rendered
Custom CSS, never copied into the fixtures. These fixtures are test-only and are
not included in any application wheel.

CDN assets used by Auth are downloaded while building the test image, verified
against `preview-assets.json` (including the original SRI hashes), and served by
Playwright interception. Browser tests and previews remain on the isolated test
network and never need live CDN, ESI, Discord, or host-agent access.
