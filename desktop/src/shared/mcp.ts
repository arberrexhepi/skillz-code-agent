export interface McpServerSettings {
  transport: 'stdio' | 'http';
  enabled?: boolean;
  command?: string;
  args?: string[];
  env?: string[];
  url?: string;
  auth?: 'none' | 'bearer' | 'oauth';
  bearerEnv?: string;
  oauth?: {
    clientId?: string;
    clientMetadataUrl?: string;
    clientSecretEnv?: string;
    tokenEndpointAuthMethod?: 'client_secret_basic' | 'client_secret_post';
    scope?: string;
    callbackPort?: number;
  };
  allowedTools?: string[];
  timeout?: number;
}

export interface McpProfile {
  ready: boolean;
  servers: Record<string, McpServerSettings & { authStatus?: 'signed-in' | 'sign-in-required'; builtIn?: boolean }>;
}

export interface McpApi {
  profile(artifactId?: string): Promise<McpProfile>;
  save(name: string, settings: McpServerSettings, artifactId?: string): Promise<McpProfile>;
  manage(action: 'enable' | 'disable' | 'remove' | 'login' | 'logout', name: string, artifactId?: string): Promise<McpProfile>;
  inspect(name: string, kind: 'tools' | 'resources' | 'templates' | 'prompts', artifactId?: string, query?: string, offset?: number): Promise<{ items: { name?: string; uri?: string; uriTemplate?: string; description?: string }[]; total: number; next_offset?: number }>;
  setup(): Promise<boolean>;
}
