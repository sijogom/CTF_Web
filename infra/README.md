# CTF_Web 인프라

Docker Compose로 PostgreSQL, Redis, MinIO, CTFd, API 서비스, 프론트엔드와 챌린지 실행 환경을 구성합니다. 배포 스크립트는 Linux를 대상으로 합니다.

## 설정

1. `.env.example`을 같은 디렉터리의 `.env`로 복사합니다.
2. DB·Redis·MinIO·관리자·내부 API의 비밀값과 사이트 도메인을 자신의 값으로 지정합니다.
3. `JWT_SECRET_KEY_SOURCE`, `JWT_PUBLIC_KEY_SOURCE`가 가리키는 키 파일을 배포 트리 밖에 준비합니다.
4. 사용할 SMTP·OAuth·Discord·CTFd 연동 값만 별도로 설정합니다.

템플릿의 빈 값과 `CHANGE_ME`는 운영 자격 증명이 아닙니다. `.env.schema`와 Compose 필수값 검증을 함께 확인합니다.

## 실행

저장소 루트에서 설치 스크립트를 검토한 뒤 Linux 환경에서 실행합니다.

```sh
bash infra/scripts/deploy.sh install
```

서비스 조작 시 기존 볼륨과 일치하는 프로젝트명 `ctf_web`을 유지합니다. 운영 오버레이는 `docker-compose.prod.yml`, 개발 설정은 `docker-compose.dev.yml`에 있습니다. `down -v` 등 데이터 삭제 명령을 일반 재시작에 사용하지 않습니다.

Auth·Learn·Community DB 마이그레이션은 각 서비스의 `migrations/README.md`와 Compose migration job 계약을 따릅니다. 실제 DB에 적용하기 전 백업과 스키마 상태를 확인합니다.

## 운영 도구

[ctfctl 사용법](scripts/ctfctl-README.md)을 참고하세요. `scripts/renewal/`의 원격 도구는 `CTF_DEPLOY_HOST`, `CTF_DEPLOY_KEY`, `CTF_KNOWN_HOSTS`를 실행 환경에서 명시해야 합니다. 기본 원격 계정은 `deploy`, SSH 포트는 10022이며, 이 도구의 서버 경로 계약을 먼저 확인해야 합니다. 저장소에는 접속 비밀이나 운영 데이터가 포함되지 않습니다.
