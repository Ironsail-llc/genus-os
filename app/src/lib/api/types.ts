export interface Person {
  id: string;
  name: { firstName: string; lastName: string };
  email?: string;
  phone?: string;
  jobTitle?: string;
  city?: string;
  company?: { name: string } | null;
  linkedinUrl?: string;
  avatarUrl?: string;
  createdAt?: string;
  updatedAt?: string;
}

export interface Company {
  id: string;
  name: string;
  domainName?: string;
  address?: string;
  employees?: number;
  linkedinUrl?: string;
  annualRecurringRevenue?: number;
  idealCustomerProfile?: boolean;
  createdAt?: string;
  updatedAt?: string;
}

export interface Conversation {
  id: number;
  status: "open" | "resolved" | "pending" | "snoozed";
  inbox_id: number;
  contact: {
    id: number;
    name: string;
    email?: string;
  };
  assignee?: {
    id: number;
    name: string;
  };
  messages_count: number;
  unread_count: number;
  last_activity_at: string;
}

export interface Message {
  id: number;
  content: string;
  message_type: "incoming" | "outgoing";
  sender?: {
    id: number;
    name: string;
    type: string;
  };
  created_at: string;
  private: boolean;
  attachments?: Array<{ file_url: string; file_type: string }>;
}

export interface MemorySearchResult {
  content: string;
  category: string;
  similarity: number;
  created_at: string;
}

export interface MemoryEntity {
  id: string;
  name: string;
  entity_type: string;
  mention_count: number;
  last_mentioned: string;
  relations?: Array<{
    relation_type: string;
    target_entity: string;
  }>;
}

export interface ServiceHealth {
  name: string;
  url: string;
  status: "healthy" | "unhealthy";
  responseTime?: number;
}

export interface HealthResponse {
  status: "ok" | "degraded";
  services: ServiceHealth[];
  timestamp: string;
}

export interface VisionStatus {
  mode: string;
  status: string;
  uptime?: number;
}
