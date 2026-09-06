// splitConcealed：流式 Markdown/LaTeX 未闭合构造探测（conceal 策略）。
import { describe, expect, it } from "vitest";
import { splitConcealed } from "../components/conceal";

describe("splitConcealed", () => {
  it("已闭合内容不产生 conceal", () => {
    expect(splitConcealed("完整文本，$x^2$ 与 $$y^2$$ 都闭合")).toEqual({
      safe: "完整文本，$x^2$ 与 $$y^2$$ 都闭合",
      concealed: "",
    });
  });

  it("未闭合行内公式尾部灰化", () => {
    expect(splitConcealed("看这个 $x^2")).toEqual({
      safe: "看这个 ",
      concealed: "$x^2",
    });
  });

  it("未闭合块级公式尾部灰化", () => {
    expect(splitConcealed("前文 $$x^2")).toEqual({
      safe: "前文 ",
      concealed: "$$x^2",
    });
  });

  it("未闭合代码块尾部灰化", () => {
    const text = "```python\nprint(1)";
    expect(splitConcealed(text)).toEqual({
      safe: "",
      concealed: text,
    });
  });

  it("未闭合 begin/end 环境尾部灰化", () => {
    expect(splitConcealed("公式 \\begin{equation} x")).toEqual({
      safe: "公式 ",
      concealed: "\\begin{equation} x",
    });
  });

  it("已闭合 begin/end 不产生 conceal", () => {
    expect(splitConcealed("\\begin{equation}x\\end{equation}好了")).toEqual({
      safe: "\\begin{equation}x\\end{equation}好了",
      concealed: "",
    });
  });

  it("行内公式按行配对：最后一行未闭合只灰化该行尾部", () => {
    expect(splitConcealed("第一行 $a$\n第二行 $b")).toEqual({
      safe: "第一行 $a$\n第二行 ",
      concealed: "$b",
    });
  });

  it("空文本不产生 conceal", () => {
    expect(splitConcealed("")).toEqual({ safe: "", concealed: "" });
  });
});
