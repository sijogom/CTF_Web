# Auth DB 마이그레이션

Auth는 Alembic 도입 전부터 존재한 core schema가 있다. 따라서 이 디렉터리는
core를 추측해 재작성하지 않고, 검증된 기존 schema를 `20260813_000` baseline에
기록한 뒤 전진 migration만 적용한다.

## 지원하는 준비 경로

Compose의 Auth schema 준비는 `python -m app.scripts.auth_migrate`만 사용한다.

1. **빈 DB**: historical core table만 생성하고 baseline(000) 계약을 검증한 뒤
   `20260813_000`을 stamp한다.
2. **완전한 unversioned legacy core**: `create_all`을 실행하지 않는다. baseline
   core table/column 계약만 검증하고 `20260813_000`을 stamp한다.
3. 두 경우 모두 `001 → 002 → 003 → head` 순서로 Alembic forward migration을
   적용한 뒤, 현재 runtime에 필요한 table/column 계약을 다시 검증한다.
4. **version marker가 있는 DB**: bootstrap DDL 없이 `upgrade head`만 수행한 뒤
   같은 post-head 계약을 검증한다.

`000` 이전에는 현재 SQLAlchemy metadata 전체를 legacy DB에 대조하지 않는다.
Discord identity, 이메일 코드 인증, 대회 scoring·soft delete, 관리자 설정 등은
각각의 forward migration이 소유한다.

다음 상태는 DDL 전에 fail-closed 한다.

- marker만 남은 DB 또는 부분 core schema
- 부분 생성된 ban ledger
- 알 수 없는 application table
- baseline에 필요한 legacy core column 누락
- `upgrade head` 뒤 현재 runtime 필수 table/column 누락

## 앱 시작과 직접 개발

Auth 애플리케이션 시작 경로는 더 이상 `create_all()` 또는 `ALTER TABLE` fallback을
실행하지 않는다. 이는 Compose migration job 실패를 앱 시작이 우회하지 못하게 하기
위한 것이다. Docker 밖 직접 개발도 먼저 다음 migration wrapper를 실행해야 한다.

```text
python -m app.scripts.auth_migrate
```

실행 전 DB 접속 설정을 준비하고 대상 데이터베이스와 현재 revision을 확인한다.

## 변경 규칙

- 새 runtime-required table/column은 forward migration과 함께
  `app/utils/auth_schema_bootstrap_contract.py`의 post-head 계약을 갱신한다.
- baseline contract에는 새 기능 컬럼을 소급 추가하지 않는다. 그렇게 하면 오래된
  완전 legacy DB가 forward migration을 적용할 기회를 잃는다.
- 데이터가 있는 ledger migration의 자동 downgrade는 지원하지 않는다. 검증된 백업
  복구 절차를 사용한다.

환경값과 DB 접속 비밀은 저장소 밖에서 주입합니다. 테스트용 DB와 운영 DB를 구분합니다.
