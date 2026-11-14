"""이메일 HTML 템플릿 — 5개 언어 지원 (ko/en/ja/zh/vi)"""

import html as _html

from app.config import settings

# ---------------------------------------------------------------------------
# 다국어 번역 딕셔너리
# ---------------------------------------------------------------------------
_T: dict[str, dict[str, str]] = {
    "ko": {
        "verification_title": "이메일 인증",
        "verification_greeting": "안녕하세요, <strong>{name}</strong>님!",
        "verification_body": "아래 버튼을 클릭하여 이메일 인증을 완료해주세요.",
        "verification_button": "이메일 인증하기",
        "verification_expiry": "이 링크는 24시간 동안 유효합니다.",
        "verification_code_body": "아래 인증 코드를 회원가입 화면에 입력하여 이메일 인증을 완료해주세요.",
        "verification_code_label": "인증 코드",
        "verification_code_expiry": "이 인증 코드는 {minutes}분 동안 유효합니다.",
        "verification_ignore": "본인이 요청하지 않았다면 이 이메일을 무시해주세요.",
        "reset_title": "비밀번호 재설정",
        "reset_greeting": "안녕하세요, <strong>{name}</strong>님!",
        "reset_body": "비밀번호 재설정을 요청하셨습니다. 아래 버튼을 클릭하여 새 비밀번호를 설정하세요.",
        "reset_button": "비밀번호 재설정",
        "reset_expiry": "이 링크는 1시간 동안 유효합니다.",
        "reset_ignore": "본인이 요청하지 않았다면 이 이메일을 무시해주세요.",
        "admin_footer": "이 메일은 CTF Platform 관리자가 발송한 메일입니다.",
        "unsubscribe": "이메일 수신 거부",
        "test_title": "SMTP 테스트 메일",
        "test_body": "이 메일은 관리자 패널에서 발송된 SMTP 설정 테스트 메일입니다.",
        "test_confirm": "이 메일이 정상적으로 수신되었다면, SMTP 설정이 올바르게 구성된 것입니다.",
        "test_footer": "CTF Platform 관리자 시스템",
        "inquiry_guest_access_subject": "[CTF Platform] 문의 접근 링크",
        "inquiry_reply_available_subject": "[CTF Platform] 문의 답변이 등록되었습니다",
        "inquiry_greeting_named": "안녕하세요, <strong>{name}</strong>님!",
        "inquiry_greeting_guest": "안녕하세요!",
        "inquiry_guest_access_body": "아래 버튼을 눌러 문의 내용을 안전하게 확인하세요.",
        "inquiry_reply_available_body": "문의에 새 답변이 등록되었습니다. 아래 버튼을 눌러 확인하세요.",
        "inquiry_access_button": "문의 확인하기",
        "inquiry_footer": "본인이 요청하지 않은 메일이라면 이 메일을 무시해주세요.",
    },
    "en": {
        "verification_title": "Email Verification",
        "verification_greeting": "Hello, <strong>{name}</strong>!",
        "verification_body": "Please click the button below to verify your email.",
        "verification_button": "Verify Email",
        "verification_expiry": "This link is valid for 24 hours.",
        "verification_code_body": "Enter the verification code below on the sign-up page to complete email verification.",
        "verification_code_label": "Verification Code",
        "verification_code_expiry": "This verification code is valid for {minutes} minutes.",
        "verification_ignore": "If you did not request this, please ignore this email.",
        "reset_title": "Password Reset",
        "reset_greeting": "Hello, <strong>{name}</strong>!",
        "reset_body": "You requested a password reset. Click the button below to set a new password.",
        "reset_button": "Reset Password",
        "reset_expiry": "This link is valid for 1 hour.",
        "reset_ignore": "If you did not request this, please ignore this email.",
        "admin_footer": "This email was sent by CTF Platform administrator.",
        "unsubscribe": "Unsubscribe",
        "test_title": "SMTP Test Email",
        "test_body": "This is a test email sent from the admin panel.",
        "test_confirm": "If you received this email, your SMTP configuration is correct.",
        "test_footer": "CTF Platform Admin System",
        "inquiry_guest_access_subject": "[CTF Platform] Inquiry access link",
        "inquiry_reply_available_subject": "[CTF Platform] A reply is available for your inquiry",
        "inquiry_greeting_named": "Hello, <strong>{name}</strong>!",
        "inquiry_greeting_guest": "Hello!",
        "inquiry_guest_access_body": "Use the button below to securely view your inquiry.",
        "inquiry_reply_available_body": "A new reply is available for your inquiry. Use the button below to view it.",
        "inquiry_access_button": "View inquiry",
        "inquiry_footer": "If you did not expect this email, you can ignore it.",
    },
    "ja": {
        "verification_title": "メール認証",
        "verification_greeting": "こんにちは、<strong>{name}</strong>さん！",
        "verification_body": "下のボタンをクリックしてメール認証を完了してください。",
        "verification_button": "メールを認証する",
        "verification_expiry": "このリンクは24時間有効です。",
        "verification_code_body": "下の認証コードを会員登録画面に入力して、メール認証を完了してください。",
        "verification_code_label": "認証コード",
        "verification_code_expiry": "この認証コードは{minutes}分間有効です。",
        "verification_ignore": "ご自身でリクエストしていない場合は、このメールを無視してください。",
        "reset_title": "パスワードリセット",
        "reset_greeting": "こんにちは、<strong>{name}</strong>さん！",
        "reset_body": "パスワードリセットがリクエストされました。下のボタンをクリックして新しいパスワードを設定してください。",
        "reset_button": "パスワードをリセット",
        "reset_expiry": "このリンクは1時間有効です。",
        "reset_ignore": "ご自身でリクエストしていない場合は、このメールを無視してください。",
        "admin_footer": "このメールはCTF Platformの管理者から送信されました。",
        "unsubscribe": "メール配信停止",
        "test_title": "SMTPテストメール",
        "test_body": "このメールは管理者パネルから送信されたSMTP設定テストメールです。",
        "test_confirm": "このメールが正常に受信された場合、SMTP設定は正しく構成されています。",
        "test_footer": "CTF Platform管理システム",
        "inquiry_guest_access_subject": "[CTF Platform] お問い合わせアクセスリンク",
        "inquiry_reply_available_subject": "[CTF Platform] お問い合わせに返信が届きました",
        "inquiry_greeting_named": "こんにちは、<strong>{name}</strong>さん！",
        "inquiry_greeting_guest": "こんにちは！",
        "inquiry_guest_access_body": "下のボタンからお問い合わせ内容を安全に確認できます。",
        "inquiry_reply_available_body": "お問い合わせに新しい返信が届きました。下のボタンから確認してください。",
        "inquiry_access_button": "お問い合わせを確認する",
        "inquiry_footer": "心当たりがない場合は、このメールを無視してください。",
    },
    "zh": {
        "verification_title": "邮箱验证",
        "verification_greeting": "您好，<strong>{name}</strong>！",
        "verification_body": "请点击下方按钮完成邮箱验证。",
        "verification_button": "验证邮箱",
        "verification_expiry": "此链接24小时内有效。",
        "verification_code_body": "请在注册页面输入下方验证码以完成邮箱验证。",
        "verification_code_label": "验证码",
        "verification_code_expiry": "该验证码在{minutes}分钟内有效。",
        "verification_ignore": "如果您没有发起此请求，请忽略此邮件。",
        "reset_title": "密码重置",
        "reset_greeting": "您好，<strong>{name}</strong>！",
        "reset_body": "您已请求密码重置。请点击下方按钮设置新密码。",
        "reset_button": "重置密码",
        "reset_expiry": "此链接1小时内有效。",
        "reset_ignore": "如果您没有发起此请求，请忽略此邮件。",
        "admin_footer": "此邮件由CTF Platform管理员发送。",
        "unsubscribe": "取消订阅",
        "test_title": "SMTP测试邮件",
        "test_body": "这是从管理面板发送的SMTP配置测试邮件。",
        "test_confirm": "如果您收到了此邮件，说明SMTP配置正确。",
        "test_footer": "CTF Platform管理系统",
        "inquiry_guest_access_subject": "[CTF Platform] 查询访问链接",
        "inquiry_reply_available_subject": "[CTF Platform] 您的查询已有回复",
        "inquiry_greeting_named": "您好，<strong>{name}</strong>！",
        "inquiry_greeting_guest": "您好！",
        "inquiry_guest_access_body": "请点击下方按钮安全查看您的查询。",
        "inquiry_reply_available_body": "您的查询已有新回复。请点击下方按钮查看。",
        "inquiry_access_button": "查看查询",
        "inquiry_footer": "如果您并未预期收到此邮件，请忽略它。",
    },
}


def _t(locale: str, key: str) -> str:
    """번역 조회 -- 없으면 ko 폴백"""
    return _T.get(locale, _T["ko"]).get(key, _T["ko"].get(key, key))


# ---------------------------------------------------------------------------
# 템플릿 함수
# ---------------------------------------------------------------------------


def email_verification_template(
    username: str,
    verify_url: str | None = None,
    *,
    verification_code: str | None = None,
    locale: str = "ko",
) -> str:
    """이메일 인증 템플릿.

    신규 가입은 코드만 제공하고, 기존 링크 인증 계정의 재발송은 verify_url을
    전달해 기존 메일 흐름을 계속 지원한다.
    """
    safe_name = _html.escape(username)
    code_section = ""
    if verification_code:
        safe_code = _html.escape(verification_code)
        code_section = f"""
        <p>{_t(locale, "verification_code_body")}</p>
        <div style="margin:24px 0;padding:16px;background:#151B2B;border:1px solid #374151;border-radius:8px;text-align:center;">
            <div style="color:#AAB0C0;font-size:12px;margin-bottom:8px;">{_t(locale, "verification_code_label")}</div>
            <div style="color:#FFFFFF;font-size:28px;font-weight:bold;letter-spacing:6px;font-family:monospace;">{safe_code}</div>
        </div>
        <p style="color:#7B7F8E;font-size:14px;">{_t(locale, "verification_code_expiry").format(minutes=settings.email_verify_code_expire_minutes)}</p>
        """
    link_section = ""
    if verify_url:
        safe_verify_url = _html.escape(verify_url)
        link_section = f"""
        <p>{_t(locale, "verification_body")}</p>
        <div style="text-align:center;margin:32px 0;">
            <a href="{safe_verify_url}" style="background:#6366F1;color:white;padding:12px 32px;border-radius:8px;text-decoration:none;font-weight:bold;">
                {_t(locale, "verification_button")}
            </a>
        </div>
        <p style="color:#7B7F8E;font-size:14px;">{_t(locale, "verification_expiry")}</p>
        """
    return f"""
    <div style="max-width:600px;margin:0 auto;font-family:sans-serif;background:#0A0E17;color:#E8E9ED;padding:32px;">
        <h1 style="color:#6366F1;font-size:24px;margin-bottom:16px;">CTF Platform</h1>
        <h2 style="font-size:20px;">{_t(locale, "verification_title")}</h2>
        <p>{_t(locale, "verification_greeting").format(name=safe_name)}</p>
        {code_section}
        {link_section}
        <p style="color:#7B7F8E;font-size:14px;">
            {_t(locale, "verification_ignore")}
        </p>
    </div>
    """


def test_email_template(locale: str = "ko") -> tuple[str, str]:
    """SMTP 테스트 메일 템플릿 — (subject, html_body) 튜플 반환."""
    subject = _t(locale, "test_title")
    html_body = f"""
    <div style="max-width:600px;margin:0 auto;font-family:sans-serif;background:#0A0E17;color:#E8E9ED;padding:32px;">
        <h1 style="color:#6366F1;font-size:24px;margin-bottom:16px;">CTF Platform</h1>
        <h2 style="font-size:20px;">{_t(locale, "test_title")}</h2>
        <p>{_t(locale, "test_body")}</p>
        <p>{_t(locale, "test_confirm")}</p>
        <p style="color:#7B7F8E;font-size:14px;margin-top:24px;">
            {_t(locale, "test_footer")}
        </p>
    </div>
    """
    return subject, html_body


def admin_email_template(
    body_html: str,
    unsubscribe_url: str | None = None,
    locale: str = "ko",
) -> str:
    """관리자 이메일 발송 래핑 템플릿 -- body_html은 서버에서 정화된 HTML"""
    unsubscribe_html = ""
    if unsubscribe_url:
        safe_unsub_url = _html.escape(unsubscribe_url)
        unsubscribe_html = f"""
        <p style="color:#7B7F8E;font-size:12px;margin-top:8px;">
            <a href="{safe_unsub_url}" style="color:#6366F1;text-decoration:underline;">{_t(locale, "unsubscribe")}</a>
        </p>"""
    return f"""
    <div style="max-width:600px;margin:0 auto;font-family:sans-serif;background:#0A0E17;color:#E8E9ED;padding:32px;">
        <h1 style="color:#6366F1;font-size:24px;margin-bottom:16px;">CTF Platform</h1>
        <div style="font-size:15px;line-height:1.6;">
            {body_html}
        </div>
        <p style="color:#7B7F8E;font-size:13px;margin-top:32px;border-top:1px solid #2A2E3A;padding-top:16px;">
            {_t(locale, "admin_footer")}
        </p>
        {unsubscribe_html}
    </div>
    """


def admin_email_preview_template(body_html: str, locale: str = "ko") -> str:
    """미리보기용 — 수신거부 링크 없이 렌더링"""
    return admin_email_template(body_html, unsubscribe_url=None, locale=locale)


def password_reset_template(username: str, reset_url: str, locale: str = "ko") -> str:
    """비밀번호 재설정 템플릿"""
    safe_name = _html.escape(username)
    safe_reset_url = _html.escape(reset_url)
    return f"""
    <div style="max-width:600px;margin:0 auto;font-family:sans-serif;background:#0A0E17;color:#E8E9ED;padding:32px;">
        <h1 style="color:#6366F1;font-size:24px;margin-bottom:16px;">CTF Platform</h1>
        <h2 style="font-size:20px;">{_t(locale, "reset_title")}</h2>
        <p>{_t(locale, "reset_greeting").format(name=safe_name)}</p>
        <p>{_t(locale, "reset_body")}</p>
        <div style="text-align:center;margin:32px 0;">
            <a href="{safe_reset_url}" style="background:#6366F1;color:white;padding:12px 32px;border-radius:8px;text-decoration:none;font-weight:bold;">
                {_t(locale, "reset_button")}
            </a>
        </div>
        <p style="color:#7B7F8E;font-size:14px;">
            {_t(locale, "reset_expiry")}<br>
            {_t(locale, "reset_ignore")}
        </p>
    </div>
    """


def inquiry_notification_template(
    kind: str,
    access_url: str,
    *,
    recipient_name: str | None = None,
    locale: str = "ko",
) -> tuple[str, str]:
    """문의 접근·답변 알림 메일을 렌더링한다.

    본문 원문을 받지 않으며, 동적 이름과 URL은 HTML 속성/본문 모두에서
    이스케이프한다. ``access_url``은 호출자가 Auth frontend origin과 검증된
    상대 경로를 결합한 값이어야 한다.
    """
    if kind not in {"guest_access", "reply_available"}:
        raise ValueError("지원하지 않는 문의 알림 종류입니다.")

    safe_access_url = _html.escape(access_url, quote=True)
    if recipient_name:
        safe_name = _html.escape(recipient_name, quote=True)
        greeting = _t(locale, "inquiry_greeting_named").format(name=safe_name)
    else:
        greeting = _t(locale, "inquiry_greeting_guest")

    subject = _t(locale, f"inquiry_{kind}_subject")
    body = _t(locale, f"inquiry_{kind}_body")
    return subject, f"""
    <div style="max-width:600px;margin:0 auto;font-family:sans-serif;background:#0A0E17;color:#E8E9ED;padding:32px;">
        <h1 style="color:#6366F1;font-size:24px;margin-bottom:16px;">CTF Platform</h1>
        <p>{greeting}</p>
        <p>{body}</p>
        <div style="text-align:center;margin:32px 0;">
            <a href="{safe_access_url}" style="background:#6366F1;color:white;padding:12px 32px;border-radius:8px;text-decoration:none;font-weight:bold;">
                {_t(locale, "inquiry_access_button")}
            </a>
        </div>
        <p style="color:#7B7F8E;font-size:14px;">{_t(locale, "inquiry_footer")}</p>
    </div>
    """
