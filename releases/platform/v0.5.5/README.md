# B-UH platform 0.5.5

Source commit: `57abe65060796a98d5c97895a8c75aa8e4881cc6`

## Changes

- **moon-tax (fix):** Allow Moon Tax's pending statuses, policy state labels, and hover text to use the shared readable theme palette while retaining standalone color fallbacks.
- **platform (fix):** Keep database dumps and backup evidence counts consistent under concurrent writes, with a bounded lock and unchanged independent restore verification.
- **platform (internal):** Exercise Auth's five built-in theme choices, panel text contrast, sticky headers, horizontal scrolling, and VPS dialog behavior on desktop and mobile with synthetic data.
- **structure-ops (fix):** Keep console table headers visible while scrolling and adapt Moon Tax, Structure Operations, Schedule, ESI Archive, and VPS Health to Auth's light and dark themes.

## Artifacts

- `aa_buh_mining_analytics-0.2.1-py3-none-any.whl` — 241807bed3805f363c0a84352de82278d16bed98f6562953feaae6784ee2a240 (reused)
- `aa_buh_moon_tax-0.3.4-py3-none-any.whl` — 0e157845fa3d8c7ff74e95994cf8961fea6768e8fcdcc0b48db0297b36e4ae9e (built)
- `aa_buh_structure_ops-0.3.3-py3-none-any.whl` — 0c4c188d3dbcd034d4df0d553fec01c2b1b99828cb30fcbfeaf803619d7e45b6 (built)
- `aa_moonmining-3.1.0.post1-py3-none-any.whl` — aa4ddbc7da64151cd6d988d971d59462360e35a5111d47a3bf56d577f454bc13 (reused)
- `aa_structures-4.0.3-py3-none-any.whl` — ba8493ef10a450198fa5ba3d8995e1ea661b74dca20d770beaa91bf005f558e3 (reused)
