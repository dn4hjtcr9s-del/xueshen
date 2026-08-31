// 全站备案页脚测试：确保备案文本、官方查询链接和公安标识不会在界面重构中丢失。
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SiteFooter } from "../SiteFooter";

describe("全站备案页脚", () => {
  it("展示并链接 ICP 与公安备案信息", () => {
    render(<SiteFooter />);

    expect(screen.getByRole("contentinfo", { name: "网站备案信息" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "豫ICP备2026040311号-1" })).toHaveAttribute(
      "href",
      "https://beian.miit.gov.cn/",
    );
    expect(screen.getByRole("link", { name: "粤公网安备44010602016952号" })).toHaveAttribute(
      "href",
      "https://beian.mps.gov.cn/#/query/webSearch?code=44010602016952",
    );
    expect(screen.getByRole("img", { name: "公安备案图标" })).toHaveAttribute("src", "/gongan-beian.png");
  });
});
