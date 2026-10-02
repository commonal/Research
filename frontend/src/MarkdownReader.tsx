import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";

type Props = { markdown: string; assetBaseUrl?: string };

export function headingSlug(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9\u4e00-\u9fff\u3000-\u303f\s-]/g, "")
    .trim()
    .replace(/\s+/g, "-");
}
/**
 * Keep legacy notes readable without enabling arbitrary raw HTML in
 * react-markdown.  New notes are normalised by the server; this small client
 * compatibility pass also fixes already-published notes that still contain
 * internal finding markers or simple MinerU HTML tables.
 */
export function normalizeReaderMarkdown(markdown: string): string {
  const withoutInternalMarkers = markdown
    .replace(/\s*\[\^finding:[^\]\r\n]+\]/g, "")
    .replace(/[（(]\s*对应\s+Findings?\s+[A-Za-z0-9:_-]+(?:\s*[、,]\s*(?:Findings?\s+)?[A-Za-z0-9:_-]+)*\s*[）)]/gi, "")
    .replace(/([。！？；])\1+/g, "$1");
  return withoutInternalMarkers.replace(/<table\b([^>]*)>([\s\S]*?)<\/table\s*>/gi, (whole, attributes: string, body: string) => {
    if (/\b(?:rowspan|colspan)\s*=/i.test(attributes) || /\b(?:rowspan|colspan)\s*=/i.test(body)) return whole;
    const rows = Array.from(body.matchAll(/<tr\b[^>]*>([\s\S]*?)<\/tr\s*>/gi)).map((row) =>
      Array.from(row[1].matchAll(/<(?:td|th)\b[^>]*>([\s\S]*?)<\/(?:td|th)\s*>/gi)).map((cell) => cleanTableCell(cell[1])),
    ).filter((row) => row.some((cell) => cell.length > 0));
    if (rows.length < 2) return whole;
    const width = rows[0].length;
    if (width < 2 || rows.some((row) => row.length !== width)) return whole;
    const separator = `| ${Array.from({ length: width }, () => "---").join(" | ")} |`;
    return [
      `| ${rows[0].join(" | ")} |`,
      separator,
      ...rows.slice(1).map((row) => `| ${row.join(" | ")} |`),
    ].join("\n");
  });
}
export function MarkdownReader({ markdown, assetBaseUrl }: Props) {
  return (
    <div className="markdown-reader" data-testid="markdown-reader">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeKatex]}
        components={{
          a: ({ children, href }) => (
            <a href={href} target="_blank" rel="noopener noreferrer">
              {children}
            </a>
          ),
          img: ({ alt, src }) => (
            <img
              alt={alt ?? ""}
              src={src?.startsWith("assets/") && assetBaseUrl ? `${assetBaseUrl}/${src.slice("assets/".length)}` : src}
              loading="lazy"
            />
          ),
          h1: ({ children }) => <h1 id={headingSlug(_text(children))}>{children}</h1>,
          h2: ({ children }) => <h2 id={headingSlug(_text(children))}>{children}</h2>,
          h3: ({ children }) => <h3 id={headingSlug(_text(children))}>{children}</h3>,
          code: ({ children }) => <code>{children}</code>,
        }}
      >
        {normalizeReaderMarkdown(markdown)}
      </ReactMarkdown>
    </div>
  );
}

function cleanTableCell(value: string): string {
  const text = decodeHtmlEntities(value.replace(/<br\s*\/?>/gi, " ").replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim());
  return text.replace(/\|/g, "\\|");
}

function decodeHtmlEntities(value: string): string {
  return value.replace(/&(#x?[0-9a-f]+|amp|lt|gt|quot|apos|nbsp);/gi, (entity, code: string) => {
    const lower = code.toLowerCase();
    if (lower === "amp") return "&";
    if (lower === "lt") return "<";
    if (lower === "gt") return ">";
    if (lower === "quot") return '"';
    if (lower === "apos") return "'";
    if (lower === "nbsp") return " ";
    const numeric = lower.startsWith("#x") ? Number.parseInt(lower.slice(2), 16) : Number.parseInt(lower.slice(1), 10);
    return Number.isFinite(numeric) && numeric >= 0 && numeric <= 0x10ffff ? String.fromCodePoint(numeric) : entity;
  });
}

/** Flatten React children (text nodes) into a plain string for heading ids. */
function _text(node: unknown): string {
  if (Array.isArray(node)) return node.map(_text).join("");
  if (node && typeof node === "object" && "props" in (node as { props?: object })) {
    const props = (node as { props?: { children?: unknown } }).props;
    return props ? _text(props.children) : "";
  }
  return String(node ?? "");
}

