# Changelog

## [4.17.0](https://github.com/gorecvpn/Gorec/compare/v4.16.9...v4.17.0) (2026-09-19)


### New Features

* accept starts_at on admin raffle campaign create ([98e5fe4](https://github.com/gorecvpn/Gorec/commit/98e5fe4e0ef890977799b5a56ec58f42d05e1f3f))
* accept starts_at on admin raffle campaign create ([4f7ba0f](https://github.com/gorecvpn/Gorec/commit/4f7ba0f178f3ebcc85f3727a195f4891b1f9564f))
* cabinet API for raffle tickets (user + admin) ([44e0ca9](https://github.com/gorecvpn/Gorec/commit/44e0ca95fbdfa94fadb5e0ad2093c6f7dceec849))
* **menu:** built-in «Розыгрыш» main-menu button (4.16.6) ([#21](https://github.com/gorecvpn/Gorec/issues/21)) ([eca25f0](https://github.com/gorecvpn/Gorec/commit/eca25f0c36a4ac6eb1d502bcc657e6e914483cbc))
* **menu:** env-gate «Розыгрыш» button via RAFFLE_BUTTON_VISIBLE (4.16.7) ([e075479](https://github.com/gorecvpn/Gorec/commit/e075479ea993f05f4c5211094bd6e6d6016b2d12))
* **menu:** RAFFLE_BUTTON_VISIBLE env gate for «Розыгрыш» (4.16.7) ([88408a0](https://github.com/gorecvpn/Gorec/commit/88408a096437a8cb357c3efa1462d6a27aeb86f8))
* MVP raffle tickets on subscription purchase ([be0d382](https://github.com/gorecvpn/Gorec/commit/be0d38201f83b9e8d2603ea9954bf876b9d8655a))
* MVP raffle tickets on subscription purchase ([6737904](https://github.com/gorecvpn/Gorec/commit/6737904d758dd2fc0efd14f82f75dc3c028c6e8b))
* raffle admin edit/delete, prize images, hide user pool ([be13c14](https://github.com/gorecvpn/Gorec/commit/be13c146a2f07edfa59efeed6f527d94d602a4ce))
* raffle admin edit/delete, prize images, hide user pool ([f424d9e](https://github.com/gorecvpn/Gorec/commit/f424d9e5a908fc380fa5f6df899ca91f5a7241a1))
* raffle draw history API + multi-winner coverage ([cf3f922](https://github.com/gorecvpn/Gorec/commit/cf3f922ba42772ebd9ddac03314deb478e9e1329))
* raffle draw history API with enriched winners ([53832cb](https://github.com/gorecvpn/Gorec/commit/53832cb6c612124334680a4514de1271a91caaf6))
* raffle MVP+ history, multi-place prizes, tickets-by-tariff ([857db8d](https://github.com/gorecvpn/Gorec/commit/857db8d0e635dab565e307089d0a2bb29ec3c9ca))
* **raffle:** admin prize image upload API (4.16.4) ([#19](https://github.com/gorecvpn/Gorec/issues/19)) ([1b9327c](https://github.com/gorecvpn/Gorec/commit/1b9327cf60de4df3a2c8513557fadfda9bfb13cc))
* sync Bedolaga upstream into Gorec (legacy tariff flow, panel identity, users segments) ([12dcdd9](https://github.com/gorecvpn/Gorec/commit/12dcdd9dbf7ec7c1bb2e97b29d1ee10348abb3d9))


### Bug Fixes

* avoid MissingGreenlet after RemnaWave sync timeout on purchase ([546dd84](https://github.com/gorecvpn/Gorec/commit/546dd84011857f7aff963ddb1e393798dc437f50))
* award raffle tickets for paid subscription purchases ([26db274](https://github.com/gorecvpn/Gorec/commit/26db274329bf3b0495492084c9db2db0e0f7e4b4))
* award raffle tickets for paid subscription purchases ([3b9a394](https://github.com/gorecvpn/Gorec/commit/3b9a3942fdd5e55ce2d36ac2e328af874ab09f93))
* cabinet update check → gorecvpn/Gorec-Cabinet ([a239cac](https://github.com/gorecvpn/Gorec/commit/a239cacde20578c04d243a7176aa4640ebc9f563))
* import _normalize_tickets_by_tariff in admin raffle create ([b4a6b79](https://github.com/gorecvpn/Gorec/commit/b4a6b7916ebacfef2b86795aac9732e78c6e3698))
* **menu:** «Розыгрыш» in-bot callback, not cabinet WebApp (4.16.8) ([dffe016](https://github.com/gorecvpn/Gorec/commit/dffe016e314358d6cc31fccdf52fb0c9f5e14b25))
* **menu:** open «Розыгрыш» in-bot instead of cabinet WebApp (4.16.8) ([a555231](https://github.com/gorecvpn/Gorec/commit/a555231a46e73b0921b0fe8fd030577bd469f4bf))
* missing import for tickets_by_tariff normalize ([f5db395](https://github.com/gorecvpn/Gorec/commit/f5db395e1a5faebf10f6af6fbceb2f8cea9619d8))
* MissingGreenlet after RemnaWave timeout on cabinet purchase ([af6566a](https://github.com/gorecvpn/Gorec/commit/af6566a1f149cf734e5723fd7acbf98a97fc00fc))
* point cabinet update check at gorecvpn/Gorec-Cabinet ([f808aef](https://github.com/gorecvpn/Gorec/commit/f808aef8a9ce47dd5956f9a1f214a076bdbb2b47))
* **raffle:** relative /uploads URLs for prize images (4.16.5) ([648c07c](https://github.com/gorecvpn/Gorec/commit/648c07cc32aa268260f1f34692626b8caf8d8aa9))
* **raffle:** return relative /uploads URLs for prize images (4.16.5) ([349d10d](https://github.com/gorecvpn/Gorec/commit/349d10d206a2bc122cb3241d8fc4ddfbca68ed83))
* renumber upstream panel-identity migration to 0127 after Gorec raffle 0125/0126 ([12dcdd9](https://github.com/gorecvpn/Gorec/commit/12dcdd9dbf7ec7c1bb2e97b29d1ee10348abb3d9))
* sync uv.lock for gorecbot package name ([7330ec0](https://github.com/gorecvpn/Gorec/commit/7330ec08e5f3bc8f8565a3d87a235a4b03dec0ae))
* sync uv.lock with package name gorecbot ([97335e1](https://github.com/gorecvpn/Gorec/commit/97335e1887be6c995d1808f768496ccdb0665a1b))
* sync uv.lock with package name gorecbot ([c050052](https://github.com/gorecvpn/Gorec/commit/c050052710b0598577e47f63434c2f749a1e39bf))


### Documentation

* clean README (remove Bedolaga promo/docs/hero) ([583a396](https://github.com/gorecvpn/Gorec/commit/583a396ac896ed80885d9a1070c5d71d231723ad))
* fix broken MIT license footer in README ([49dad1f](https://github.com/gorecvpn/Gorec/commit/49dad1f9dc7861bfffb2b21f87bbcb1519287f57))
* fix README MIT footer ([5542925](https://github.com/gorecvpn/Gorec/commit/5542925b27733b6a9659d6932a21d2a772c6fb21))
* refresh project structure reference for raffle button flag ([b7a3ecb](https://github.com/gorecvpn/Gorec/commit/b7a3ecbdf381ce9afc1b2779f690764eba7db3da))
* refresh project structure reference for raffle in-bot menu ([3f53f87](https://github.com/gorecvpn/Gorec/commit/3f53f872a11ee7a8ed2a5df1ca604df64c66c318))
* regenerate project structure reference after upstream sync ([12dcdd9](https://github.com/gorecvpn/Gorec/commit/12dcdd9dbf7ec7c1bb2e97b29d1ee10348abb3d9))
* remove Bedolaga promo, docs links, and hero from README ([e49973c](https://github.com/gorecvpn/Gorec/commit/e49973c2b209d29e5d274d8150811a5c4029c3fa))
* remove Bedolaga provenance footer from README ([4dbaa41](https://github.com/gorecvpn/Gorec/commit/4dbaa41d31e8af6d2b1ddc1099ead5e2393a281f))
* remove provenance footer ([506db48](https://github.com/gorecvpn/Gorec/commit/506db486283bbb8637a94b1b7dd8ba6decbe7700))
