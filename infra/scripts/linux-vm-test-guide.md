# 리눅스 VM 테스트 가이드

실제 서버 배포 전 VM에서 먼저 테스트하는 방법.

---

## 1. VM 내부에서만 테스트 (간단)

### .env 설정
```
SITE_DOMAIN=localhost
FRONTEND_URL=https://localhost
```

### 실행
```bash
cd CTF_Web
sed -i 's/\r$//' infra/scripts/deploy.sh
bash infra/scripts/deploy.sh install
```

### 확인
```bash
# 헬스체크
curl -s http://localhost:8020/health   # auth
curl -s http://localhost:8100/health   # learn
curl -s http://localhost:8300/health   # community
curl -s http://localhost:8200/health   # env-orchestrator
curl -s http://localhost:3100          # frontend

# HTTPS (자체 서명 인증서이므로 -k 필요)
curl -sk https://localhost
```

### 제한사항
- Windows 브라우저에서 접속 불가 (VM 터미널에서만 확인 가능)
- UI 확인은 VM에 데스크톱 환경 설치 후 브라우저 사용

---

## 2. Windows 브라우저에서 VM 접속

### VM IP 확인
```bash
ip addr show | grep "inet " | grep -v 127.0.0.1
# 예: 192.168.56.101
```

### .env 설정
```
SITE_DOMAIN=192.168.56.101
FRONTEND_URL=https://192.168.56.101
```

### 실행
```bash
bash infra/scripts/deploy.sh install
```

### Windows에서 접속
```
https://192.168.56.101
```
- 브라우저 경고 뜸 (Caddy 자체 서명 인증서) → "고급" → "계속 진행" 클릭
- Chrome: `thisisunsafe` 타이핑으로 우회 가능

### Caddyfile 동작 원리
```
:80 { abort }              ← IP 직접 접속 차단 (보안)
{$SITE_DOMAIN} { ... }    ← SITE_DOMAIN과 일치하는 요청만 서빙
```
SITE_DOMAIN=IP로 설정하면 해당 IP로 들어온 요청을 Caddy가 처리함.

---

## 3. 도메인으로 테스트 (hosts 파일)

DNS 없이 가짜 도메인으로 테스트하는 방법.

### Windows hosts 파일 수정 (관리자 권한)
```
C:\Windows\System32\drivers\etc\hosts
```
아래 줄 추가:
```
192.168.56.101  test.example.com
```

### .env 설정
```
SITE_DOMAIN=test.example.com
FRONTEND_URL=https://test.example.com
```

### Windows에서 접속
```
https://test.example.com
```
- 자체 서명 인증서 경고는 동일하게 뜸
- 실제 도메인과 동일한 환경으로 테스트 가능

### 테스트 끝나면 hosts 파일에서 해당 줄 삭제할 것

---

## 4. 테스트 체크리스트

### 기본 동작
- [ ] deploy.sh install 정상 완료
- [ ] 헬스체크 5개 서비스 전부 OK
- [ ] HTTPS 접속 가능

### 인증
- [ ] 회원가입 (이메일 인증 — SMTP 설정에 따라 Mailpit 또는 실제 메일)
- [ ] 로그인 / 로그아웃
- [ ] 관리자 계정 로그인 (.env의 ADMIN_EMAIL / ADMIN_PASSWORD)

### 핵심 기능
- [ ] 워게임 목록 조회
- [ ] 워게임 문제 풀이 (flag 제출)
- [ ] 커뮤니티 게시글 작성/수정/삭제
- [ ] 장터 아이템 등록/수정/삭제
- [ ] 프로필 페이지 접속
- [ ] 랭킹 페이지 접속
- [ ] 관리자 대시보드 접속

### 챌린지 환경 (env-orchestrator)
- [ ] 챌린지 환경 시작 (Docker 컨테이너 생성)
- [ ] 웹 터미널 접속
- [ ] 환경 종료

---

## 5. VM 테스트 후 실서버 전환

테스트 완료 후 실서버에 배포할 때 변경할 값:

```
SITE_DOMAIN=실제도메인.ac.kr
FRONTEND_URL=https://실제도메인.ac.kr
SMTP_HOST=실제SMTP서버
SMTP_PORT=587
SMTP_TLS=true
SMTP_USER=실제계정
SMTP_PASSWORD=실제비밀번호
ADMIN_EMAIL=실제관리자@도메인
```

Windows hosts 파일에 추가한 테스트 도메인이 있으면 삭제할 것.
