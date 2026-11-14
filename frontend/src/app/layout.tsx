import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "CUHA Memory",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html suppressHydrationWarning>
      <head>
        {/*
          Pretendard Variable v1.3.9: SIL Open Font License 1.1.
          한·일·중 CJK 표기와 일반 본문을 하나의 검증된 계열로 처리한다.
          아래 CSS의 실제 family 이름은 "Pretendard Variable"이다.
        */}
        <link
          rel="stylesheet"
          as="style"
          crossOrigin="anonymous"
          href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css"
        />
        {/* JetBrains Mono: SIL Open Font License 1.1, 코드·플래그 입력 전용 */}
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&display=swap"
          rel="stylesheet"
        />
        {/* 테마 깜박임 방지: React 하이드레이션 전에 테마 클래스를 즉시 적용 */}
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{var s=JSON.parse(localStorage.getItem("ctf-a11y-settings"));var t=s&&s.themeMode||"light";if(t==="system")t=matchMedia("(prefers-color-scheme:dark)").matches?"dark":"light";document.documentElement.classList.add(t);document.documentElement.style.colorScheme=t}catch(e){document.documentElement.classList.add("light");document.documentElement.style.colorScheme="light"}})()`,
          }}
        />
      </head>
      <body>{children}</body>
    </html>
  );
}
