-- 최소 DB 스키마 (IMPLEMENTATION_PLAN.md 2장). 시험은 SQLite, 운영 DB 확정 후 이식한다.

-- ① GCC 원천
CREATE TABLE IF NOT EXISTS collection_run (
  collection_id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_type   TEXT NOT NULL DEFAULT 'GCC',
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  base_date     TEXT NOT NULL,
  status        TEXT NOT NULL CHECK (status IN ('성공','실패','불완전')),
  is_baseline   INTEGER NOT NULL DEFAULT 0,
  raw_ref       TEXT,
  error_message TEXT,
  change_count  INTEGER NOT NULL DEFAULT 0,
  new_case_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS gcc_record (
  collection_id INTEGER NOT NULL REFERENCES collection_run(collection_id),
  gcc_code      TEXT NOT NULL,
  org_name      TEXT NOT NULL,
  parent_code   TEXT,
  prev_org_code TEXT,
  created_date  TEXT,
  abolished_date TEXT,
  raw_json      TEXT,
  PRIMARY KEY (collection_id, gcc_code)
);

-- 기관 식별 (GCC 코드와 OG 전거ID는 구분 보존, 1:1 강제 없음. COMMON = 기존 자료의 공통기관ID)
CREATE TABLE IF NOT EXISTS org (
  org_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  org_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS org_identifier (
  org_id   INTEGER NOT NULL REFERENCES org(org_id),
  id_type  TEXT NOT NULL CHECK (id_type IN ('GCC','OG','COMMON')),
  id_value TEXT NOT NULL,
  PRIMARY KEY (id_type, id_value)
);

-- 변경 건
CREATE TABLE IF NOT EXISTS review_case (
  case_id         INTEGER PRIMARY KEY AUTOINCREMENT,
  primary_code    TEXT NOT NULL,
  title           TEXT NOT NULL,
  change_summary  TEXT NOT NULL,
  detected_at     TEXT NOT NULL,
  analysis_status TEXT NOT NULL DEFAULT '대기' CHECK (analysis_status IN ('대기','분석 중','완료','실패')),
  data_status     TEXT NOT NULL DEFAULT '정상' CHECK (data_status IN ('정상','자료 부족','원천 충돌')),
  analysis_attempts INTEGER NOT NULL DEFAULT 0,
  last_error      TEXT
);

CREATE TABLE IF NOT EXISTS change_event (
  change_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id       INTEGER NOT NULL REFERENCES review_case(case_id),
  change_key    TEXT NOT NULL UNIQUE,
  change_type   TEXT NOT NULL,
  gcc_code      TEXT NOT NULL,
  field_name    TEXT NOT NULL,
  old_value     TEXT,
  new_value     TEXT,
  before_collection_id INTEGER REFERENCES collection_run(collection_id),
  after_collection_id  INTEGER REFERENCES collection_run(collection_id),
  detected_at   TEXT NOT NULL
);

-- ② 법령 근거
CREATE TABLE IF NOT EXISTS law_source (
  law_source_id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id       INTEGER NOT NULL REFERENCES review_case(case_id),
  law_name      TEXT NOT NULL,
  law_ident     TEXT,
  version_ident TEXT,
  role          TEXT NOT NULL CHECK (role IN ('개편 전','개편 후')),
  effective_date TEXT,
  source_url    TEXT,
  fetch_status  TEXT NOT NULL DEFAULT '확보',
  fetched_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS law_provision (
  provision_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  law_source_id INTEGER NOT NULL REFERENCES law_source(law_source_id),
  article_ref   TEXT NOT NULL,
  is_addendum   INTEGER NOT NULL DEFAULT 0,
  text          TEXT NOT NULL
);

-- ③ 분석 후보
CREATE TABLE IF NOT EXISTS relation_candidate (
  candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id      INTEGER NOT NULL REFERENCES review_case(case_id),
  change_id    INTEGER REFERENCES change_event(change_id),
  from_code    TEXT NOT NULL,
  to_code      TEXT NOT NULL,
  from_name    TEXT,
  to_name      TEXT,
  relation_type TEXT NOT NULL,
  valid_from   TEXT,
  valid_to     TEXT,
  date_status  TEXT NOT NULL CHECK (date_status IN ('확인','미확인','원천 충돌')),
  proposed_json TEXT NOT NULL,
  draft_json   TEXT,
  review_status TEXT NOT NULL DEFAULT '승인 대기' CHECK (review_status IN ('승인 대기','승인 완료','보류','기각'))
);
CREATE TABLE IF NOT EXISTS task_move_candidate (
  candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id      INTEGER NOT NULL REFERENCES review_case(case_id),
  change_id    INTEGER REFERENCES change_event(change_id),
  from_code    TEXT NOT NULL,
  to_code      TEXT NOT NULL,
  from_name    TEXT,
  to_name      TEXT,
  task_description TEXT NOT NULL,
  move_scope   TEXT NOT NULL CHECK (move_scope IN ('전체','일부','범위 미확인')),
  effective_date TEXT,
  date_status  TEXT NOT NULL CHECK (date_status IN ('확인','미확인','원천 충돌')),
  proposed_json TEXT NOT NULL,
  draft_json   TEXT,
  review_status TEXT NOT NULL DEFAULT '승인 대기' CHECK (review_status IN ('승인 대기','승인 완료','보류','기각'))
);
CREATE TABLE IF NOT EXISTS evidence (
  evidence_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_type TEXT NOT NULL CHECK (candidate_type IN ('relation','task_move')),
  candidate_id   INTEGER NOT NULL,
  provision_id   INTEGER NOT NULL REFERENCES law_provision(provision_id),
  excerpt        TEXT,
  reason         TEXT
);

-- 로그인 계정. 승인자·수정자는 이 계정의 고정 user_id 로 자동 기록한다.
-- 계정은 삭제하지 않고 is_active 로 비활성화하여 과거 기록을 보존한다.
CREATE TABLE IF NOT EXISTS app_user (
  user_id       INTEGER PRIMARY KEY AUTOINCREMENT,
  login_id      TEXT NOT NULL UNIQUE,
  display_name  TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  is_active     INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL,
  last_login_at TEXT
);

-- ⑤ 검토 이력 (추가만 한다)
CREATE TABLE IF NOT EXISTS review_log (
  review_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_type TEXT NOT NULL,
  candidate_id   INTEGER NOT NULL,
  action         TEXT NOT NULL CHECK (action IN ('승인','수정 후 승인','보류','기각')),
  before_value   TEXT,
  after_value    TEXT,
  reason         TEXT,
  reviewer_id    INTEGER NOT NULL REFERENCES app_user(user_id),
  reviewed_at    TEXT NOT NULL
);

-- ④ 확정 데이터 (is_current = 현재 유효한 값, 정정 시 새 행을 만들고 기존 행은 보존)
CREATE TABLE IF NOT EXISTS confirmed_org_info (
  confirmed_id INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id       INTEGER NOT NULL REFERENCES org(org_id),
  org_name     TEXT NOT NULL,
  parent_org_id INTEGER REFERENCES org(org_id),
  valid_from   TEXT,
  valid_to     TEXT,
  date_status  TEXT NOT NULL,
  source_candidate_id INTEGER,
  source_candidate_type TEXT,
  approved_by  INTEGER NOT NULL REFERENCES app_user(user_id),
  approved_at  TEXT NOT NULL,
  is_current   INTEGER NOT NULL DEFAULT 1,
  supersedes_id INTEGER REFERENCES confirmed_org_info(confirmed_id)
);
CREATE TABLE IF NOT EXISTS confirmed_relation (
  confirmed_id INTEGER PRIMARY KEY AUTOINCREMENT,
  from_org_id  INTEGER NOT NULL REFERENCES org(org_id),
  to_org_id    INTEGER NOT NULL REFERENCES org(org_id),
  relation_type TEXT NOT NULL,
  valid_from   TEXT,
  valid_to     TEXT,
  date_status  TEXT NOT NULL,
  source_candidate_id INTEGER NOT NULL,
  approved_by  INTEGER NOT NULL REFERENCES app_user(user_id),
  approved_at  TEXT NOT NULL,
  is_current   INTEGER NOT NULL DEFAULT 1,
  supersedes_id INTEGER REFERENCES confirmed_relation(confirmed_id)
);
CREATE TABLE IF NOT EXISTS confirmed_task_move (
  confirmed_id INTEGER PRIMARY KEY AUTOINCREMENT,
  from_org_id  INTEGER NOT NULL REFERENCES org(org_id),
  to_org_id    INTEGER NOT NULL REFERENCES org(org_id),
  task_description TEXT NOT NULL,
  move_scope   TEXT NOT NULL,
  effective_date TEXT,
  date_status  TEXT NOT NULL,
  source_candidate_id INTEGER NOT NULL,
  approved_by  INTEGER NOT NULL REFERENCES app_user(user_id),
  approved_at  TEXT NOT NULL,
  is_current   INTEGER NOT NULL DEFAULT 1,
  supersedes_id INTEGER REFERENCES confirmed_task_move(confirmed_id)
);
CREATE TABLE IF NOT EXISTS correction_log (
  correction_id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_type   TEXT NOT NULL,
  target_id     INTEGER NOT NULL,
  new_target_id INTEGER NOT NULL,
  old_value     TEXT NOT NULL,
  new_value     TEXT NOT NULL,
  reason        TEXT NOT NULL,
  corrected_by  INTEGER NOT NULL REFERENCES app_user(user_id),
  corrected_at  TEXT NOT NULL
);

-- 비밀이 아닌 설정만 저장한다 (접속 비밀값은 환경변수 등에 둔다).
CREATE TABLE IF NOT EXISTS app_setting (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 엑셀 가져오기(참고 자료). 'ref_' 테이블은 외부 파일 값을 원본 상태·출처 그대로 보존한 참고용이며
-- 우리 시스템의 승인 결과가 아니고, 일자별 GCC 수집본(collection_run/gcc_record)도 아니다.
-- 확정 데이터·검토 이력·후보 테이블에는 쓰지 않는다.
CREATE TABLE IF NOT EXISTS import_run (
  import_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  file_name   TEXT NOT NULL,
  file_sha256 TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  report_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ref_org_record (
  source_row_id TEXT PRIMARY KEY,
  origin        TEXT NOT NULL DEFAULT 'imported_reference',
  import_id     INTEGER NOT NULL REFERENCES import_run(import_id),
  org_id        INTEGER REFERENCES org(org_id),
  common_org_id TEXT,
  source_system TEXT NOT NULL,
  source_code   TEXT,
  full_name     TEXT,
  lowest_name   TEXT,
  depth         TEXT,
  parent_code   TEXT,
  top_code      TEXT,
  previous_code TEXT,
  source_start  TEXT,
  source_end    TEXT,
  source_status TEXT,
  master_status_src TEXT,
  period_status_src TEXT,
  source_file   TEXT,
  source_sheet  TEXT,
  raw_source_id TEXT,
  raw_locator   TEXT
);

CREATE TABLE IF NOT EXISTS ref_org_work (
  work_id       TEXT PRIMARY KEY,
  origin        TEXT NOT NULL DEFAULT 'imported_reference',
  import_id     INTEGER NOT NULL REFERENCES import_run(import_id),
  org_id        INTEGER REFERENCES org(org_id),
  common_org_id TEXT,
  og_code_raw   TEXT,
  representative_name TEXT,
  raw_work_text TEXT,
  raw_start     TEXT,
  raw_end       TEXT,
  source_id     TEXT,
  source_file   TEXT,
  source_page   TEXT,
  source_sequence TEXT,
  brm_function_status TEXT,
  work_valid_from TEXT,
  work_valid_to   TEXT,
  work_period_status TEXT,
  work_adoption_status TEXT,
  law_reference_raw TEXT
);

CREATE TABLE IF NOT EXISTS ref_org_relation (
  relation_id   TEXT PRIMARY KEY,
  origin        TEXT NOT NULL DEFAULT 'imported_reference',
  import_id     INTEGER NOT NULL REFERENCES import_run(import_id),
  layer         TEXT NOT NULL,
  relation_type TEXT,
  from_org_id   INTEGER REFERENCES org(org_id),
  to_org_id     INTEGER REFERENCES org(org_id),
  from_common_org_id TEXT,
  to_common_org_id   TEXT,
  from_source_code TEXT,
  to_source_code   TEXT,
  source_system TEXT,
  source_field  TEXT,
  observed_from TEXT,
  observed_to   TEXT,
  relation_valid_from TEXT,
  relation_valid_to   TEXT,
  review_status_src TEXT,
  ai_use_policy_src TEXT,
  evidence      TEXT,
  date_status_src TEXT
);
