// plotly.js-dist-min ships no type declarations; we only ever pass it
// through react-plotly.js's factory (which types the Plotly instance as
// `unknown`), so a bare ambient module declaration is all that's needed.
declare module 'plotly.js-dist-min'
