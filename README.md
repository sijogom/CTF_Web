# CTF_Web

이론 학습과 CTF 실습을 연결하는 웹 플랫폼입니다. 강의·지식 태그·선행조건, 워게임·대회, 사용자·팀·커뮤니티 관리 기능을 포함합니다.

## 구성

| 경로 | 역할 |
|---|---|
| [frontend](frontend/README.md) | Next.js·React·TypeScript, 한국어·영어·일본어·중국어 UI |
| `services/auth` | 인증·권한·사용자·팀·대회 |
| `services/learn` | 강의·챌린지·학습 경로 |
| `services/community` | 게시판·문의·커뮤니티 |
| `services/env-orchestrator` | Go 기반 챌린지 컨테이너 관리 |
| [infra](infra/README.md) | Docker Compose·PostgreSQL·Redis·MinIO·프록시 |
| `ctfd` | CTFd 이미지 설정 |

## 시작 전 준비

Linux 서버와 Docker Compose 배포를 기본으로 합니다. [인프라 README](infra/README.md)의 설정을 먼저 준비합니다. 환경 파일 위치는 `infra/.env`이며 실제 DB 비밀번호·JWT 키·관리자 인증 값은 직접 설정해야 합니다.

```powershell
git clone https://github.com/sijogom/CTF_Web.git
cd CTF_Web
Copy-Item infra/.env.example infra/.env
```

이후 자신의 환경값과 비밀 파일을 준비한 뒤 Linux에서 `infra/scripts/deploy.sh`의 설치·실행 절차를 사용합니다. 운영 데이터가 있는 환경에는 초기화 명령을 실행하지 않습니다.

## 서비스 설정

회원가입 허용 도메인은 `services/auth/app/schemas/auth.py`의 `ALLOWED_EMAIL_DOMAINS`에서 관리합니다. 배포 환경의 가입 정책에 맞게 확인합니다.

강의·챌린지·대회 콘텐츠는 관리자 기능으로 등록합니다. DB 마이그레이션과 운영 명령은 각 서비스 및 인프라 README를 참고하세요.
