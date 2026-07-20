# Changelog

## [0.15.0](https://github.com/chiva/labelito/compare/v0.14.1...v0.15.0) (2026-07-20)


### Features

* **web:** visual drag-and-drop label builder in the Studio ([#82](https://github.com/chiva/labelito/issues/82)) ([3dca06f](https://github.com/chiva/labelito/commit/3dca06fee7ae985a6ac79e9f5b2d4578ea3fc1ac))

## [0.14.1](https://github.com/chiva/labelito/compare/v0.14.0...v0.14.1) (2026-07-18)


### Bug Fixes

* **web:** wrap Studio toolbar buttons instead of overflowing when narrow ([#80](https://github.com/chiva/labelito/issues/80)) ([41c24c1](https://github.com/chiva/labelito/commit/41c24c14e8ce364cb841db0935bb319f3fc921b5))

## [0.14.0](https://github.com/chiva/labelito/compare/v0.13.0...v0.14.0) (2026-07-18)


### Features

* **web:** show render-option pills on history rows ([#78](https://github.com/chiva/labelito/issues/78)) ([27f52fd](https://github.com/chiva/labelito/commit/27f52fd9629997dc633601b57efcb148797a7bf1))


### Bug Fixes

* **web:** keep Studio toolbar on one row when the page is re-expanded ([#77](https://github.com/chiva/labelito/issues/77)) ([df9c103](https://github.com/chiva/labelito/commit/df9c103f54506237465503bce7ad2c8143d6107e))

## [0.13.0](https://github.com/chiva/labelito/compare/v0.12.0...v0.13.0) (2026-07-17)


### Features

* **devcontainer:** compose-based dev container that works on SELinux/rootless-podman hosts ([#70](https://github.com/chiva/labelito/issues/70)) ([ca2a18c](https://github.com/chiva/labelito/commit/ca2a18c62be5d65fcdb88f2c3a31da0e0cae8138))
* print the current draft directly from the studio ([#75](https://github.com/chiva/labelito/issues/75)) ([90e6c7d](https://github.com/chiva/labelito/commit/90e6c7d5d35b789fee3438102754de082238f40f))


### Bug Fixes

* **web:** guard large sequence prints with in-page dialog, not native confirm() ([#76](https://github.com/chiva/labelito/issues/76)) ([e479b28](https://github.com/chiva/labelito/commit/e479b28f3b2f0d611275b85b5b75cccbfdf3e395))

## [0.12.0](https://github.com/chiva/labelito/compare/v0.11.0...v0.12.0) (2026-07-11)


### Features

* warn when TEMPLATES_WRITABLE=true but the templates dir is read-only ([#66](https://github.com/chiva/labelito/issues/66)) ([c5bcf39](https://github.com/chiva/labelito/commit/c5bcf390a68c32c5cd10e982543499c2fa3898d0))


### Bug Fixes

* **i18n:** merge user translation catalogs onto bundled ones key-by-key ([#65](https://github.com/chiva/labelito/issues/65)) ([e96b4c3](https://github.com/chiva/labelito/commit/e96b4c3e7e13e2b97f7e13152c05480ab2e8bf78))
* surface an unreachable printer as a clean 503, not a raw-errno 500 ([#63](https://github.com/chiva/labelito/issues/63)) ([2c55708](https://github.com/chiva/labelito/commit/2c557086553a8a55afd4ab76f3e95bb0117f1c87))

## [0.11.0](https://github.com/chiva/labelito/compare/v0.10.0...v0.11.0) (2026-07-10)


### Features

* "today" date label + localized month names (%b/%B) ([#60](https://github.com/chiva/labelito/issues/60)) ([70770e5](https://github.com/chiva/labelito/commit/70770e5db5ebce01d490c0c2b2bbffe927a5176f))

## [0.10.0](https://github.com/chiva/labelito/compare/v0.9.0...v0.10.0) (2026-07-10)


### Features

* **api:** accept inline (on-the-fly) template body on /print and /preview ([#58](https://github.com/chiva/labelito/issues/58)) ([905a688](https://github.com/chiva/labelito/commit/905a6883935318a50a242f7b7c52bbb70ce9d498))

## [0.9.0](https://github.com/chiva/labelito/compare/v0.8.1...v0.9.0) (2026-07-10)


### Features

* **ui:** blink key icon red when a stored API token is rejected ([#56](https://github.com/chiva/labelito/issues/56)) ([6353041](https://github.com/chiva/labelito/commit/635304103e0eae09e890612cc6790e562cbfc29b))

## [0.8.1](https://github.com/chiva/labelito/compare/v0.8.0...v0.8.1) (2026-07-09)


### Bug Fixes

* **ui:** drop ellipsis from Printing badge, remove pencil-edit legend hint ([#53](https://github.com/chiva/labelito/issues/53)) ([aac285f](https://github.com/chiva/labelito/commit/aac285fdbf0558d4675d2f093311960735f456a5))

## [0.8.0](https://github.com/chiva/labelito/compare/v0.7.0...v0.8.0) (2026-07-09)


### Features

* **ui:** dashed border for example templates + README hero & badges ([#50](https://github.com/chiva/labelito/issues/50)) ([7d55bd8](https://github.com/chiva/labelito/commit/7d55bd8121a6c1bfbe5563be3ed95fd18bf714d9))


### Bug Fixes

* **status:** converge Connection badge and stop end-of-print Error flash ([#52](https://github.com/chiva/labelito/issues/52)) ([ae0a477](https://github.com/chiva/labelito/commit/ae0a477693d49c9fabed18271545d56726a2eae8))

## [0.7.0](https://github.com/chiva/labelito/compare/v0.6.0...v0.7.0) (2026-07-09)


### Features

* optional HTTP Basic auth + About modal revamp with update check ([#48](https://github.com/chiva/labelito/issues/48)) ([5cbf0dc](https://github.com/chiva/labelito/commit/5cbf0dc3883d77c8ce76afcb4936307dd88f9ed0))
* optional HTTP Basic auth + single shared API-token entry ([#47](https://github.com/chiva/labelito/issues/47)) ([2585a8e](https://github.com/chiva/labelito/commit/2585a8edcae65bca81a7d5e7a09fc245c5f2e090))
* severity-aware printer status notices + About/version modal ([#45](https://github.com/chiva/labelito/issues/45)) ([b97d1bc](https://github.com/chiva/labelito/commit/b97d1bc3304d90a27bfb93c6e17fa2d3d20afa54))

## [0.6.0](https://github.com/chiva/labelito/compare/v0.5.0...v0.6.0) (2026-07-08)


### Features

* add downloadable brand-asset kit and fix light-card mark ([#43](https://github.com/chiva/labelito/issues/43)) ([2a79b62](https://github.com/chiva/labelito/commit/2a79b62c0f136593893539e76752a422727d11b8))
* bigger centered die-cut address labels + template valign ([#31](https://github.com/chiva/labelito/issues/31)) ([192b8d8](https://github.com/chiva/labelito/commit/192b8d8666f260acadca75ea7f86d5be70387ec5))
* landscape die-cut address labels for 17x54 and 29x90 ([#28](https://github.com/chiva/labelito/issues/28)) ([6d6868a](https://github.com/chiva/labelito/commit/6d6868a53baafec1cdfdb4e21325ae69dbaf2e76))
* sequence (auto-numbering) batches in the web UI ([#41](https://github.com/chiva/labelito/issues/41)) ([8d9eb78](https://github.com/chiva/labelito/commit/8d9eb78ad19e22f202a0f5e103ae84c821c35659))
* unified CSS-style per-side padding for every element ([#34](https://github.com/chiva/labelito/issues/34)) ([462d7b0](https://github.com/chiva/labelito/commit/462d7b08bab2383608c7839f7cbc43ada22ccedb))
* web UI image upload for label image fields ([#29](https://github.com/chiva/labelito/issues/29)) ([ffe50d2](https://github.com/chiva/labelito/commit/ffe50d23bfe9fc76ad908f13ff5b668edf2ddc22))


### Bug Fixes

* give gallery label paper a gray backing for contrast ([#44](https://github.com/chiva/labelito/issues/44)) ([9e5a93d](https://github.com/chiva/labelito/commit/9e5a93dbf42bf6be50761f97be1713fed5092da1))
* production-audit batch 1 — data-dir fail-fast, e2e in CI, /print smoke test ([#35](https://github.com/chiva/labelito/issues/35)) ([be32081](https://github.com/chiva/labelito/commit/be32081151eff13bb36758b1890cf105d990ee9f))
* production-audit batch 2 — preview 422s + threadpool, padding degradation, history versioning, /readyz healthchecks ([#37](https://github.com/chiva/labelito/issues/37)) ([1d48616](https://github.com/chiva/labelito/commit/1d4861614edde4051081cf57b541f39fac37487e))
* production-audit batch 4 — lifespan, LOG_LEVEL, load-time media check, strict gallery icons, threadpool lookups, network coverage ([#38](https://github.com/chiva/labelito/issues/38)) ([1acf398](https://github.com/chiva/labelito/commit/1acf39882ddcc6c6378c11af08f8fa9f592efd4a))
* unify home/gallery header nav and improve gallery discoverability ([#42](https://github.com/chiva/labelito/issues/42)) ([bbc3538](https://github.com/chiva/labelito/commit/bbc353821f518bfeddcbeb16736d10b202bde08d))


### Documentation

* record the latch-gate blind spot and the deferred observability counter ([#40](https://github.com/chiva/labelito/issues/40)) ([9128018](https://github.com/chiva/labelito/commit/9128018b7c7f694237cea466fe862632f62dfbb5))

## [0.5.0](https://github.com/chiva/labelito/compare/v0.4.0...v0.5.0) (2026-07-06)


### Features

* column/list layout primitives + text fill/border/divider ([#24](https://github.com/chiva/labelito/issues/24)) ([0ed416b](https://github.com/chiva/labelito/commit/0ed416b9fe05cbd96cec0c06da01985707f5cc5d))
* label gallery page with a rendered example per template ([#26](https://github.com/chiva/labelito/issues/26)) ([f8b1cb5](https://github.com/chiva/labelito/commit/f8b1cb5fdc7a4d59e1ab361713220bab2c454394))
* per-template 'Edit in Studio' pencil on the Print page ([#25](https://github.com/chiva/labelito/issues/25)) ([66221e4](https://github.com/chiva/labelito/commit/66221e45edad9982b824abadebd64c91f3f3b94a))
* studio draft preview parity with print page ([#23](https://github.com/chiva/labelito/issues/23)) ([a9414d6](https://github.com/chiva/labelito/commit/a9414d6cd703a0332ce4e93f533b0ea6be9ea747))


### Documentation

* add logo to README header ([#20](https://github.com/chiva/labelito/issues/20)) ([1205ae1](https://github.com/chiva/labelito/commit/1205ae12a9d3efc1ab5a48c32ed91e65d7615c7c))

## [0.4.0](https://github.com/chiva/labelito/compare/v0.3.0...v0.4.0) (2026-07-05)


### Features

* sample icon/image templates, missing-icon boot warning, slimmer image ([#18](https://github.com/chiva/labelito/issues/18)) ([a638072](https://github.com/chiva/labelito/commit/a638072e78c58f3fe944d8c1501693fa06f8f62c))

## [0.3.0](https://github.com/chiva/labelito/compare/v0.2.0...v0.3.0) (2026-07-04)


### Features

* studio tables, example templates, print/docker polish, Python 3.13 floor ([#14](https://github.com/chiva/labelito/issues/14)) ([4d2dde1](https://github.com/chiva/labelito/commit/4d2dde169b1534045748fd95cd47adaaa2bf2642))


### Bug Fixes

* **deps:** update dependency @fortawesome/fontawesome-free to v7 ([#15](https://github.com/chiva/labelito/issues/15)) ([3d205af](https://github.com/chiva/labelito/commit/3d205af36ec3fb8723bf8fcc28e6bc8c46fa0a8f))

## [0.2.0](https://github.com/chiva/labelito/compare/v0.1.3...v0.2.0) (2026-07-04)


### Features

* initial release ([#4](https://github.com/chiva/labelito/issues/4)) ([a3a13d7](https://github.com/chiva/labelito/commit/a3a13d7965a49e7328353d8a36193bf12889771a))

## [0.1.3](https://github.com/chiva/labelito/compare/v0.1.2...v0.1.3) (2026-06-27)


### Bug Fixes

* docker buildx for cache ([#20](https://github.com/chiva/labelito/issues/20)) ([d1a9a73](https://github.com/chiva/labelito/commit/d1a9a7348036d39359ae2dee295a52f3d48bcb19))

## [0.1.2](https://github.com/chiva/labelito/compare/v0.1.1...v0.1.2) (2026-06-27)


### Bug Fixes

* ci errors and release pipeline ([#18](https://github.com/chiva/labelito/issues/18)) ([141c7fd](https://github.com/chiva/labelito/commit/141c7fd18a8eb439b905a7dc3d71050bb33a26b9))

## [0.1.1](https://github.com/chiva/labelito/compare/v0.1.0...v0.1.1) (2026-06-26)


### Bug Fixes

* Update dependency @material-symbols/svg-400 to ^0.45.0 ([#5](https://github.com/chiva/labelito/issues/5)) ([f6ae572](https://github.com/chiva/labelito/commit/f6ae5727b1cd17c2a8f877d7ca75a43e3a664654))

## 0.1.0 (2026-06-26)


### Features

* Create CODEOWNERS ([#2](https://github.com/chiva/labelito/issues/2)) ([e2e3962](https://github.com/chiva/labelito/commit/e2e3962efe09e39b06d2c78eaa1ff18791361b88))
