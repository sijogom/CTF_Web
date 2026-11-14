export interface AttachmentInfo {
  filename: string;
  original_name: string;
  size: number;
  content_type: string;
  object_path: string;
}

export interface Lesson {
  id: string;
  title: string;
  order_index: number;
  estimated_minutes: number;
  challenge_id: number | null;
  attachments?: AttachmentInfo[];
}
