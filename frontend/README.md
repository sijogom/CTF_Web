# CTF_Web 프론트엔드

Next.js App Router, React, TypeScript, Tailwind CSS를 사용합니다. 번역은 `messages/`의 한국어·영어·일본어·중국어 파일에서 관리합니다.

## 개발

이 디렉터리에서 실행합니다.

```powershell
npm ci
npm run dev -- --port 3100
```

API 전달 경로와 서비스 호스트·포트는 `next.config.ts`를 확인합니다. 인증·학습·커뮤니티 API 서비스를 함께 실행합니다. 실제 비밀값은 `NEXT_PUBLIC_*` 환경변수에 넣지 않습니다.

## 검사와 빌드

```powershell
npm run lint
node --test tests/*.test.mjs
npm run build
```

개별 Node 테스트가 추가 패키지를 요구하면 먼저 `npm ci`를 완료합니다. 운영 실행 전 사이트 URL, API 전달 경로, 프록시 하위 경로를 자신의 배포 환경과 맞춥니다. 실제 사용자·콘텐츠 데이터는 API에서 조회합니다.
