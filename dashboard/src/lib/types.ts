export interface StudentMetrics {
  student_id: string;
  key_preview: string;
  student_name: string;
  total_tokens_consumed: number;
  is_active: boolean;
  created_at: string;
  updated_at: string;
  monthly_token_ceiling: number;
  tokens_remaining: number;
  token_percent_used: number;
  standard_window_count: number;
  standard_requests_remaining: number;
  standard_burst_remaining: number;
  high_speed_window_count: number;
  high_speed_requests_remaining: number;
  high_speed_burst_remaining: number;
  rate_window_seconds: number;
  burst_window_seconds: number;
}

export interface UsersPayload {
  users: StudentMetrics[];
  monthly_token_ceiling: number;
  standard_window_limit: number;
  high_speed_window_limit: number;
  rate_window_seconds: number;
  standard_burst_limit: number;
  high_speed_burst_limit: number;
  burst_window_seconds: number;
}
