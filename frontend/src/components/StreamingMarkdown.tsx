// StreamingMarkdown：流式回答的 Markdown/LaTeX 渲染（conceal 策略）。
//
// 流式期间通过 splitConcealed 把未闭合构造尾部灰化显示；终态（非 streaming）
// 不启用 conceal，直接全量渲染。
import ReactMarkdown from "react-markdown";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { splitConcealed } from "./conceal";

export default function StreamingMarkdown({
  text,
  streaming,
}: {
  text: string;
  streaming: boolean;
}) {
  const rendered = (content: string) => (
    <ReactMarkdown remarkPlugins={[remarkMath]} rehypePlugins={[rehypeKatex]}>
      {content}
    </ReactMarkdown>
  );
  if (!streaming) return rendered(text);
  const { safe, concealed } = splitConcealed(text);
  return (
    <>
      {rendered(safe)}
      {concealed ? <span className="conceal-tail">{concealed}</span> : null}
    </>
  );
}
