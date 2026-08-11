import type { MessageBlock } from '@/types';
import type { ChatStyle } from '@/stores/appStore';

/**
 * 分行渲染 action 与 dialogue 内容块（PRD §3.5，验收标准 19）。
 *
 * - action：角色动作、神态、心理活动与场景描写，浅色斜体，独立成行
 * - dialogue：角色台词，正常字体与主要文字颜色，独立成行
 *
 * 不自动为动作添加括号，也不自动为台词添加引号；块之间保留固定间距。
 * 实时聊天、历史会话与数据导出共用这一套渲染。
 */

interface Props {
  blocks: MessageBlock[];
  chatStyle?: ChatStyle;
  /** 流式生成中的最后一块后面显示光标 */
  streaming?: boolean;
}

export default function MessageBlocks({ blocks, chatStyle = '沉浸叙事', streaming = false }: Props) {
  const bubble = chatStyle === '经典气泡';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      {blocks.map((block, i) => {
        const isLast = i === blocks.length - 1;
        const caret = streaming && isLast ? <span className="stream-caret" /> : null;

        if (block.type === 'action') {
          return (
            <div key={block.id} className="block-action">
              {block.content}
              {caret}
            </div>
          );
        }

        return (
          <div key={block.id}>
            <div className={bubble ? 'block-dialogue--bubble' : 'block-dialogue'}>
              {block.content}
              {caret}
            </div>
          </div>
        );
      })}
    </div>
  );
}
