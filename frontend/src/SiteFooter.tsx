// 全站备案页脚：展示工信部 ICP 备案与公安联网备案信息，并链接至官方查询平台。
const ICP_RECORD = "豫ICP备2026040311号-1";
const PUBLIC_SECURITY_RECORD = "粤公网安备44010602016952号";
const PUBLIC_SECURITY_CODE = "44010602016952";

export function SiteFooter() {
  return (
    <footer className="site-footer" aria-label="网站备案信息">
      <div className="site-footer-row">
        <a href="https://beian.miit.gov.cn/" target="_blank" rel="noopener noreferrer">
          {ICP_RECORD}
        </a>
      </div>
      <div className="site-footer-row">
        <a
          className="site-footer-public-security"
          aria-label={PUBLIC_SECURITY_RECORD}
          href={`https://beian.mps.gov.cn/#/query/webSearch?code=${PUBLIC_SECURITY_CODE}`}
          target="_blank"
          rel="noopener noreferrer"
        >
          <img src="/gongan-beian.png" alt="公安备案图标" />
          <span>{PUBLIC_SECURITY_RECORD}</span>
        </a>
      </div>
    </footer>
  );
}
