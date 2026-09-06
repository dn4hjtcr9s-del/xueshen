// splitConcealed：流式 Markdown/LaTeX 未闭合构造探测（conceal 策略）。
//
// 未闭合构造（代码块 ```、块级公式 $$...$$、\begin{...}、行内 $...$）
// 的尾部按原文灰化显示（.conceal-tail），闭合后立即正常渲染，避免
// "先纯文本后变公式" 的闪烁与半截公式裸奔。
export interface ConcealedSplit {
  safe: string;
  concealed: string;
}

/** 找到第一个未闭合构造的起点，把文本切成"已闭合"与"未闭合尾部"。 */
export function splitConcealed(text: string): ConcealedSplit {
  if (!text) return { safe: "", concealed: "" };
  // 1) fenced code block：``` 出现奇数次 → 未闭合（最外层优先，内部的
  //    $$/$/\begin 不作为独立构造处理）。
  if (text.split("```").length % 2 === 0) {
    const idx = text.lastIndexOf("```");
    return { safe: text.slice(0, idx), concealed: text.slice(idx) };
  }
  // 2) 块级公式 $$...$$：$$ 出现奇数次 → 未闭合。
  if (text.split("$$").length % 2 === 0) {
    const idx = text.lastIndexOf("$$");
    return { safe: text.slice(0, idx), concealed: text.slice(idx) };
  }
  // 3) \begin{...} 未闭合：最后一个 \begin{ 之后没有 \end{。
  const beginIdx = text.lastIndexOf("\\begin{");
  const endIdx = text.lastIndexOf("\\end{");
  if (beginIdx > endIdx) {
    return { safe: text.slice(0, beginIdx), concealed: text.slice(beginIdx) };
  }
  // 4) 行内 $...$：按行配对（公式不会跨行）。
  const lastLine = text.slice(text.lastIndexOf("\n") + 1);
  if (lastLine.split("$").length % 2 === 0) {
    const idx = text.lastIndexOf("$");
    return { safe: text.slice(0, idx), concealed: text.slice(idx) };
  }
  return { safe: text, concealed: "" };
}
