declare module '@novnc/novnc/core/rfb' {
  interface RFBOptions {
    /** WebSocket sub-protocols to request (default: ['binary']) */
    wsProtocols?: string[];
    /** Shared mode — multiple clients can connect simultaneously */
    shared?: boolean;
    /** Credentials for VNC authentication */
    credentials?: { username?: string; password?: string; target?: string };
  }

  interface ConnectEvent {
    detail: Record<string, never>;
  }

  interface DisconnectEvent {
    detail: { clean: boolean };
  }

  interface SecurityFailureEvent {
    detail: { status: number; reason: string };
  }

  interface ClipboardEvent {
    detail: { text: string };
  }

  type RFBEventMap = {
    connect: ConnectEvent;
    disconnect: DisconnectEvent;
    credentialsrequired: Event;
    securityfailure: SecurityFailureEvent;
    clipboard: ClipboardEvent;
    bell: Event;
    desktopname: Event;
    capabilities: Event;
  };

  export default class RFB {
    constructor(target: HTMLElement, url: string, options?: RFBOptions);

    /* ---------- Properties ---------- */
    /** Scale the remote display to fit the container */
    scaleViewport: boolean;
    /** Request the remote server to resize to the container size */
    resizeSession: boolean;
    /** Clip (hide) parts of the remote display outside the container */
    clipViewport: boolean;
    /** Show a dot cursor when the remote cursor is not visible */
    showDotCursor: boolean;
    /** Enable/disable viewOnly mode */
    viewOnly: boolean;
    /** Quality level for JPEG encoding (0-9) */
    qualityLevel: number;
    /** Compression level (0-9) */
    compressionLevel: number;
    /** Background color of the canvas */
    background: string;
    /** Capabilities of the connection */
    readonly capabilities: { power: boolean };

    /* ---------- Methods ---------- */
    /** Disconnect from the VNC server */
    disconnect(): void;
    /** Send credentials for VNC authentication */
    sendCredentials(credentials: { username?: string; password?: string; target?: string }): void;
    /** Send key event */
    sendKey(keysym: number, code: string | null, down?: boolean): void;
    /** Send Ctrl+Alt+Del */
    sendCtrlAltDel(): void;
    /** Focus the VNC canvas */
    focus(): void;
    /** Blur the VNC canvas */
    blur(): void;

    /* ---------- Events ---------- */
    addEventListener<K extends keyof RFBEventMap>(
      event: K,
      handler: (e: RFBEventMap[K]) => void,
    ): void;
    removeEventListener<K extends keyof RFBEventMap>(
      event: K,
      handler: (e: RFBEventMap[K]) => void,
    ): void;
  }
}
