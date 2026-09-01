import DOMPurify from 'dompurify';
import { marked } from 'marked';
import { useMemo } from 'react';

marked.use({
  gfm: true,
  breaks: true,
});

export function MarkdownMessage({ content }: { content: string }): React.JSX.Element {
  const html = useMemo(() => DOMPurify.sanitize(marked.parse(content, { async: false }) as string, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ['style', 'iframe', 'object', 'embed', 'form'],
    FORBID_ATTR: ['style'],
  }), [content]);

  return <div className="markdown-message" dangerouslySetInnerHTML={{ __html: html }} />;
}
