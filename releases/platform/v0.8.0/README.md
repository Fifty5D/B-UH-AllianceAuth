# B-UH platform 0.8.0

Source commit: `ffe6e1b1834f0c550cff001a2b7c14cd6839256e`

## Changes

- **esi-history (fix):** Support Alliance Auth 5.4 and preserve complete public archive responses when EVE Ref catalogs lag.
- **mining-analytics (internal):** Validate Mining Analytics against the pinned Alliance Auth 5.4 runtime.
- **moon-tax (internal):** Validate Moon Tax against the pinned Alliance Auth 5.4 runtime.
- **platform (feature):** Prepare the pinned Alliance Auth 5.4.0 runtime and independent private host diagnostics.
- **platform (fix):** Keep previous-release web slots on an isolated read-only static snapshot while candidate assets are collected.
- **platform (fix):** Record bounded private evidence when a deployment finds changed previous-release static files.
- **platform (fix):** Keep the historical recovery harness test runnable after release synchronization consumes its reviewed change fragment.
- **structure-ops (internal):** Validate Structure Operations against the pinned Alliance Auth 5.4 runtime.

## Artifacts

- `aa_buh_max_history-2.1.1-py3-none-any.whl` — cae23b4a4165eea1e47a916270a84b513d501f2744bf2943f7f566dc97ab3c24 (built)
- `aa_buh_mining_analytics-0.2.3-py3-none-any.whl` — 4e649f6ff3baec5e456ad3a4720a3b1a30191c5398db11ee43e366360373ab13 (built)
- `aa_buh_moon_tax-0.3.5-py3-none-any.whl` — 33d0312f0f62d71e314553f62c0252dcc0149d5fcb9076cc86d468bcf842121d (built)
- `aa_buh_structure_ops-0.3.6-py3-none-any.whl` — cd4967ce1ade37bdc65a95a41f1fd62a7566f4fc6d736818f9251dfdd3a1d107 (built)
- `aa_moonmining-3.1.0.post1-py3-none-any.whl` — aa4ddbc7da64151cd6d988d971d59462360e35a5111d47a3bf56d577f454bc13 (reused)
- `aa_structures-4.0.3-py3-none-any.whl` — ba8493ef10a450198fa5ba3d8995e1ea661b74dca20d770beaa91bf005f558e3 (reused)
