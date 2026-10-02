const katex = require("katex");
const formulas = {
  reward: "\\begin{array} { c } { { R = 0 . 6 0 S + 0 . 2 0 E - 0 . 2 0 P - 0 . 0 5 U + 0 . 1 0 H } } \\\\ { { { } } } \\\\ { { - 0 . 7 5 B - 0 . 0 5 F _ { u } - 0 . 1 2 F _ { r } . } } \\end{array}\\tag{5}",
  safe: "\\mathrm { S A F E S U C C E S S } ( \\tau , x ) = \\mathbb { X } [ S = 1 \\land E \\geq e _ { x } \\land V _ { x } = 1 \\land O _ { \\tau }\\tag{4}",
  z: "\\begin{array} { r l } & { \\mathbf { z } ( a _ { t } ) = [ z _ { \\mathrm { w r i t e } } , z _ { \\mathrm { e x e c } } , z _ { \\mathrm { e x t e r n a l } } , } \\\\ & { \\qquad z _ { \\mathrm { s e c r e t } } , z _ { \\mathrm { s c o p e } } , z _ { \\mathrm { p e r s i s t e n t } } ] \\in [ 0 , 1 ] ^ { 6 } . } \\end{array}\\tag{1}",
};
for (const [name, f] of Object.entries(formulas)) {
  try {
    const t = katex.renderToString(f, { throwOnError: false, displayMode: true, errorColor: "#cc0000" });
    const hasError = t.includes("#cc0000") || /katex-error/.test(t);
    console.log(`[${name}] len=${t.length} katexError=${hasError}`);
    if (hasError) {
      const m = t.match(/class="([^"]*katex-error[^"]*)"/);
      if (m) console.log("  error el:", m[1].slice(0, 120));
    }
    // count any math token leftover like "0 . 6 0" rendered as separate
    console.log("  sample:", t.replace(/<[^>]+>/g, " ").trim().slice(0, 80));
  } catch (e) {
    console.log(`[${name}] THREW:`, e.message.slice(0, 200));
  }
}
