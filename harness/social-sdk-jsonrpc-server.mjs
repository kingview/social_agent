import Schema from '@deepseek-ai/schemastery'
import { admitEncodedImages } from '@deepseek-ai/dsh-attachment'
import { JsonRpcLineTransport } from '@deepseek-ai/dsh-sdk-protocol'
import { HarnessSdkJsonRpcServer } from '@deepseek-ai/dsh-sdk-jsonrpc-server'

const name = 'social-sdk-jsonrpc-server'
const inject = ['agents', 'attachments']
const Config = Schema.object({ maxTokensAsSuccess: Schema.boolean().default(false) })

function isEncodedImage(block) {
  return block?.type === 'image' && typeof block.data === 'string'
}

class MultimodalHarnessSdkJsonRpcServer extends HarnessSdkJsonRpcServer {
  async prompt(params) {
    const blocks = Array.isArray(params?.contentBlocks) ? params.contentBlocks : []
    const images = blocks.filter(isEncodedImage)
    if (images.length === 0) return super.prompt(params)

    const attachments = this.ctx.get('attachments')
    if (attachments === undefined) {
      throw new Error('SDK image prompt requires the Harness attachment store')
    }
    const refs = await admitEncodedImages(
      attachments,
      images.map((image) => ({
        data: image.data,
        mediaType: image.mimeType,
        ...(typeof image.name === 'string' ? { name: image.name } : {}),
      })),
    )
    let next = 0
    const durable = blocks.map((block) => isEncodedImage(block)
      ? { type: 'image', attachment: refs[next++] }
      : block)
    return super.prompt({ ...params, contentBlocks: durable })
  }
}

function apply(ctx, config) {
  const rootFiber = ctx.root.fiber
  const input = config.input ?? process.stdin
  const output = config.output ?? process.stdout
  const exit = config.exit ?? ((code) => process.exit(code))
  const transport = new JsonRpcLineTransport(input, output)
  const server = new MultimodalHarnessSdkJsonRpcServer(ctx, transport, {
    maxTokensAsSuccess: config.maxTokensAsSuccess,
  })
  let exitTask
  const disposeAndExit = () => {
    exitTask ??= (async () => {
      await Promise.allSettled([Promise.resolve().then(() => transport.flush())])
      await Promise.allSettled([Promise.resolve().then(() => rootFiber.dispose())])
      exit(0)
    })()
    return exitTask
  }

  transport.onRequest(async (method, params) => {
    if (method === 'initialize') await ctx.get('loader')?.await()
    const result = await server.handleRequest(method, params)
    if (method === 'shutdown') setImmediate(() => { disposeAndExit() })
    return result
  })
  ctx.effect(() => {
    transport.start()
    return async () => {
      await server.shutdown()
      transport.close()
    }
  }, 'jsonrpc.serve')
}

export { Config, apply, inject, name }
