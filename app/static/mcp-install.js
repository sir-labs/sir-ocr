(() => {
  const client = document.getElementById('mcp-client');
  const command = document.getElementById('mcp-command');
  const help = document.getElementById('mcp-help');
  const copy = document.getElementById('mcp-copy');
  const status = document.getElementById('mcp-copy-status');
  const configs = {
    claude: {
      command: command.textContent,
      help: 'รันคำสั่งนี้ แล้วเปิด Claude Code ใหม่',
    },
    codex: {
      command: "export SIR_PAT='sirpat_REPLACE_ME'\ncodex mcp add sir-ocr --url https://mcp.sir-labs.com/mcp --bearer-token-env-var SIR_PAT\ncodex",
      help: 'รันใน Terminal เดียวกันเพื่อให้ Codex อ่าน SIR_PAT ได้ เมื่อเปิด Terminal ใหม่ ให้ตั้ง SIR_PAT อีกครั้งก่อนเปิด Codex',
    },
    cursor: {
      command: JSON.stringify({mcpServers: {'sir-ocr': {
        url: 'https://mcp.sir-labs.com/mcp',
        headers: {Authorization: 'Bearer sirpat_REPLACE_ME'},
      }}}, null, 2),
      help: 'เพิ่ม sir-ocr ใน mcpServers ของไฟล์ ~/.cursor/mcp.json (เก็บ server เดิมไว้) แทน token แล้วเปิดการเชื่อมต่อ MCP ในหน้า Customize ของ Cursor',
    },
  };
  client.addEventListener('change', () => {
    command.textContent = configs[client.value].command;
    help.textContent = configs[client.value].help;
    copy.textContent = client.value === 'cursor' ? 'คัดลอก config' : 'คัดลอกคำสั่ง';
    status.textContent = '';
  });
  copy.addEventListener('click', async () => {
    const text = command.textContent;
    try {
      await navigator.clipboard.writeText(text);
      status.textContent = 'คัดลอกแล้ว — แทน sirpat_REPLACE_ME ด้วย token ของคุณก่อนใช้งาน';
    } catch {
      const range = document.createRange();
      range.selectNodeContents(command);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      status.textContent = 'คัดลอกอัตโนมัติไม่ได้ เลือกข้อความให้แล้ว กด Ctrl+C หรือ ⌘C เพื่อคัดลอก';
    }
  });
})();
