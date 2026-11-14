# ctfctl 운영 도구

`ctfctl`은 Linux에서 CTF_Web의 사전 점검, 설치, 백업, 복구, 릴리스 전환을 처리하는 Bash 도구입니다. 소스 기반 설치에는 `deploy.sh`, 검증된 불변 릴리스 운영에는 `ctfctl`을 사용합니다.

## 경로와 실행

```sh
bash /opt/ctf-web/current/infra/scripts/ctfctl help
```

| 항목 | 기본 경로 |
|---|---|
| 릴리스 저장 위치 | `/opt/ctf-web/releases` |
| 활성 릴리스 링크 | `/opt/ctf-web/current` |
| 작업 상태 기록 | `/var/lib/ctf-web/ctfctl` |
| 운영 환경 파일 | `/etc/ctf-web/ctf-web.env` |
| 백업 계약 | `/etc/ctf-web/backup-contract.env` |

Compose 프로젝트명은 `ctf_web`입니다. 경로 옵션에는 절대 경로를 사용하고 전역 옵션은 하위 명령 앞에 둡니다.

## 명령

```text
ctfctl preflight [--release <absolute-release-dir>]
ctfctl install [--release-root <absolute-dir>] --confirm install:<release-root-basename>
ctfctl init-jwt-secrets --confirm init-jwt-secrets
ctfctl backup --output <absolute-new-bundle-dir>
ctfctl restore --bundle <absolute-bundle-dir> --confirm <exact-bundle-id>
ctfctl migrate --release <absolute-release-dir> --backup <absolute-bundle-dir> --confirm migrate:<release-id>:<bundle-id>
ctfctl deploy --release <absolute-release-dir> --backup <absolute-bundle-dir> --confirm deploy:<release-id>:<bundle-id>
ctfctl smoke-test --base-url <https://host[/ctf]>
ctfctl rollback --release <absolute-release-dir> [--restore <absolute-bundle-dir>] --confirm <exact-rollback-confirmation>
```

상태를 변경하는 명령에는 대상과 일치하는 `--confirm` 값이 필요합니다. `restore`는 DB와 볼륨 데이터를 교체하므로 복구 대상과 백업을 확인하고 실행합니다.

## 릴리스 준비

릴리스는 `infra/release-manifest.json`과 `infra/release-files.sha256`으로 파일 무결성을 확인합니다. Compose 이미지는 digest로 고정하고 서버 측 `build:`를 포함하지 않습니다. 릴리스·manifest·Compose 파일은 root 소유이며 group/other 쓰기를 허용하지 않습니다.

`preflight`는 Docker·Compose 접근, 환경 키, manifest, 이미지, 영속 볼륨, `ctf-challenges` 네트워크를 읽기 전용으로 확인합니다. 네트워크는 `bridge`와 `172.30.0.0/16` 계약을 사용하며, 없거나 설정이 다르면 중단합니다.

`migrate`, `deploy`, `rollback`에는 CTFd 마이그레이션 및 런타임 계약을 검사하는 보호 장치가 있습니다. `CTFD_MIGRATION_EVIDENCE_REQUIRED`가 반환되면 강제로 우회하지 않고 해당 릴리스의 CTFd 계약을 확인합니다. `rollback`은 릴리스의 `.ctfctl-rollback-safe`에 정확한 `release_id`와 `database_backward_compatible=true`도 요구합니다.

## 인증 키

새 서버에서 `init-jwt-secrets`로 키를 생성한 뒤 환경 파일에는 키 경로만 지정합니다. 기존 키가 있으면 덮어쓰지 않습니다.

```dotenv
JWT_SECRET_KEY_SOURCE=/etc/ctf-web/secrets/jwt_private.pem
JWT_PUBLIC_KEY_SOURCE=/etc/ctf-web/secrets/jwt_public.pem
```

키 본문과 비밀번호는 소스·릴리스에 포함하지 않습니다. 기존 키의 교체는 세션 및 암호화 데이터의 호환성을 확인한 뒤 수행합니다.

## 백업과 복구

백업 계약 파일은 다음과 같이 영속 데이터 위치만 선언합니다.

```dotenv
CONTRACT_VERSION=2
POSTGRES_BACKUP_MODE=logical_pg_dumpall
CTFD_DATABASE=ctfd_db
CTFD_UPLOAD_MODE=named_volume
CTFD_UPLOAD_VOLUME=ctf_web_ctfd_uploads
CTFD_UPLOAD_MOUNT=/var/uploads
PERSISTENT_VOLUME_LOGICAL_NAMES=redis_data,minio_data,caddy_data,caddy_config,ctfd_uploads
```

백업·복구 시 PostgreSQL은 실행하고, 쓰기를 수행하는 앱·CTFd·MinIO·Redis·프록시 서비스는 정지해야 합니다. 도구는 계약과 실제 볼륨·마운트·DB 이름을 대조합니다.

백업은 `postgres/all-databases.sql`, `volumes/*.tar`, `checksums.sha256`, `manifest.json`을 생성합니다. 출력은 `ctf-web-backup-`으로 시작하는 새 디렉터리를 지정하며 기존 백업을 덮어쓰지 않습니다. archive helper 이미지는 사전에 준비합니다.

복구는 manifest, 체크섬, Compose 소유권과 정지 상태를 확인한 뒤 수행합니다. 실패 시 자동 재시작이나 볼륨 삭제를 수행하지 않습니다.

## HTTP 점검

```sh
bash infra/scripts/ctfctl smoke-test --base-url https://ctf.example.com/ctf
```

`smoke-test`는 `/health`, `/ctf`, `/ctf/ko`와 다음 학습 맵 경로를 확인합니다.

- `/ctf/ko/learn?view=map`
- `/ctf/api/learn/knowledge-map/v2/overview`
- `/ctf/api/learn/knowledge-map/v2/canvas`
- `/ctf/api/learn/knowledge-map/v2/search?q=course&limit=1`

캔버스 응답의 `graph_revision`, `nodes`, `edges`, `layout_hints`, `applied_filters`, `next_cursor`와 검색 응답 구조를 검사합니다. 공개 검색 결과가 있으면 해당 항목의 focus 응답도 확인합니다. JSON 처리에는 Python 3가 필요합니다.
