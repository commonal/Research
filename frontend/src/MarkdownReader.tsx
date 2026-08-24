import ReactMarkdown from "react-markdown";

type Props = { markdown: string; assetBaseUrl?: string };

export function MarkdownReader({ markdown, assetBaseUrl }: Props) {
  return (
    <div className="markdown-reader" data-testid="markdown-reader">
      <ReactMarkdown
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
          code: ({ children }) => <code>{children}</code>,
        }}
      >
        {markdown}
      </ReactMarkdown>
    </div>
  );
}
