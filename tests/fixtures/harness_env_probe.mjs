// Exercise the installed upstream MCP transport with the production YAML env.
import { readFileSync } from 'node:fs';
import YAML from '../../harness/node_modules/yaml/dist/index.js';
import { apply } from '../../harness/node_modules/@deepseek-ai/dsh-mcp-client/lib/index.js';

const composition = YAML.parse(readFileSync(process.argv[2], 'utf8'), {
  customTags: [{
    tag: 'tag:yaml.org,2002:js',
    resolve: value => Function('process', `return (${value})`)(process),
  }],
});
const config = composition.find(item => item.id === 'social-tools').config;
const cleanup = [];
const definitions = [];
const context = {
  root: {},
  logger: { info() {}, warn() {}, error: message => console.error(message) },
  effect(callback) { cleanup.push(callback()); },
  tools: { register(definition) { definitions.push(definition); return () => {}; } },
};
try {
  await apply(context, { ...config, reconnect: { enabled: false } });
  if (definitions.length !== 1) throw new Error('Expected exactly one probe tool');
  console.log(definitions[0].description);
} finally {
  for (const dispose of cleanup.reverse()) await dispose?.();
}
