import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown from "react-markdown";

const markdown = '<script>window.__unsafe = true</script>\n\n[source:paper:method:1]\n\n[外部来源](https://example.com)';
const html = renderToStaticMarkup(
  createElement(
    ReactMarkdown,
    {
      components: {
        a: ({ children, href }) => createElement(
          "a",
          { href, target: "_blank", rel: "noopener noreferrer" },
          children,
        ),
      },
    },
    markdown,
  ),
);

if (html.includes("<script")) {
  throw new Error("Raw script content reached executable HTML.");
}
if (!html.includes("&lt;script&gt;")) {
  throw new Error("Raw script markup was not escaped as inert text.");
}
if (!html.includes("[source:paper:method:1]") || html.includes('href="source:paper:method:1"')) {
  throw new Error("A knowledge anchor was converted into a location link.");
}
if (!html.includes('rel="noopener noreferrer"')) {
  throw new Error("External links are missing safe rel attributes.");
}

console.log("Markdown security check passed.");
