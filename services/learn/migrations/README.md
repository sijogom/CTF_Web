# Learn DB 마이그레이션 운영 원칙

- `alembic upgrade head`는 앱 워커보다 먼저 한 번만 실행한다.
- 기존 Learn DB는 현재 테이블·제약·인덱스·난이도 값의 읽기 전용 사전점검과 백업이 끝난 뒤에만 이력을 적용한다.
- 이 디렉터리의 최초 `legacy_baseline` revision은 기존 핵심 테이블을 재작성하지 않는다. 새 DB의 기존 핵심 테이블은 현재 앱의 개발 호환 bootstrap 경로가 만들고, 이후 신규 DDL은 명시 revision으로 추가한다.
- `knowledge_relation_ledger` revision은 새 관계 원장 테이블만 다룬다. 기존 태그·선행조건·추천 데이터를 자동 이관하거나 삭제하지 않는다.
- `course_difficulty_contract` revision은 기존 워게임식 난이도 값을 교육 콘텐츠 4단계로 명시 매핑한다. 알려지지 않은 값이 있으면 중단하며, 이 매핑은 비가역이므로 downgrade가 아니라 백업 복구를 사용한다.
- 관계 데이터가 한 건이라도 있으면 해당 revision의 downgrade는 중단한다. 롤백은 백업 복구 또는 관계를 `retired`로 바꾸는 운영 절차를 우선한다.
- 운영 DB의 실제 적용, stamp, downgrade, backup, restore는 대상 DB와 실행 범위를 확인한 운영 절차에서 수행한다.

환경값과 DB 접속 비밀은 저장소 밖에서 주입합니다. 테스트용 DB와 운영 DB를 구분합니다.
