pdfjs-dist 4.10.38 (Apache-2.0), from registry.npmjs.org, self-hosted for /desk/live/ and /desk/read/ PDF rendering.
One change from the published pdf.min.js (2026-11-08): the canvas drawing slice in CanvasGraphics.executeOperatorList,
"Date.now()+15", reads "Date.now()+(globalThis.pdfjsSliceMs||15)". With nothing set it is the library's 15 ms; the
Crawler (/desk/read/) sets 4, so a page drawn one slice a frame leaves the frame time for the spider. Re-apply it on an
update of the library (site/server/tests/test_pdf_draw_pacing.py checks it).
