export interface AdminSettings {
  site_name: string;
  registration_open: boolean;
  max_containers: number;
  container_timeout: number;
  source?: string;
  cache_refresh_pending?: boolean;
}

export interface AdminSettingsUpdate {
  site_name: string;
  registration_open: boolean;
  max_containers: number;
  container_timeout: number;
}

export interface SmtpSettings {
  smtp_host: string;
  smtp_port: number;
  smtp_from: string;
  smtp_tls: boolean;
  smtp_user: string;
  smtp_password_masked: string;
  source?: string;
  cache_refresh_pending?: boolean;
}

export interface SmtpSettingsUpdate {
  smtp_host: string;
  smtp_port: number;
  smtp_from: string;
  smtp_tls: boolean;
  smtp_user: string;
  smtp_password: string;
}

export interface SettingsSaveResponse {
  message: string;
  settings: AdminSettings;
  cache_refresh_pending: boolean;
}
