import os
from dotenv import load_dotenv
from pyrogram import Client
from pyrogram.types import InlineQueryResultArticle, InputTextMessageContent

from core import (
    MESSAGES_COLLECTION,
    deepseek_client,
    embed_texts,
    qdrant,
)

load_dotenv()

# Pyrogram Bot Client
bot = Client(
    "inline_bot",
    api_id=int(os.getenv("TELEGRAM_API_ID")),
    api_hash=os.getenv("TELEGRAM_API_HASH"),
    bot_token=os.getenv("BOT_TOKEN")
)

@bot.on_inline_query()
async def answer_inline_query(client, inline_query):
    query_text = inline_query.query.strip()
    if not query_text:
        return

    try:
        # Step 1: Embed search query (offloaded to a thread)
        query_vector = (await embed_texts([query_text]))[0].tolist()

        # Step 2: Search top 5 relevant messages in Qdrant
        search_response = qdrant.query_points(
            collection_name=MESSAGES_COLLECTION,
            query=query_vector,
            limit=5
        )
        search_results = search_response.points

        if not search_results:
            return

        results = []

        # Step 3: Build RAG context for DeepSeek
        context_lines = [
            f"- [{hit.payload['sender_name']} in {hit.payload['chat_title']}]: {hit.payload['text']}"
            for hit in search_results
        ]
        context_str = "\n".join(context_lines)

        try:
            ai_response = await deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {
                        "role": "system",
                        "content": "You are a concise assistant summarizing facts retrieved from a user's Telegram chat history. Answer accurately using only the context provided."
                    },
                    {
                        "role": "user",
                        "content": f"Context messages:\n{context_str}\n\nQuestion: {query_text}"
                    }
                ],
                max_tokens=250
            )
            rag_answer = ai_response.choices[0].message.content
        except Exception as e:
            rag_answer = f"Could not generate AI summary: {e}"

        # Card 1: AI Synthesized Answer
        results.append(
            InlineQueryResultArticle(
                title="🤖 DeepSeek AI Summary",
                description=rag_answer[:100] + "...",
                input_message_content=InputTextMessageContent(
                    f"**🤖 Answer for:** *\"{query_text}\"*\n\n"
                    f"{rag_answer}\n\n"
                    f"*(Synthesized from {len(search_results)} relevant messages)*"
                )
            )
        )

        # Cards 2-6: Individual Matched Messages
        for hit in search_results:
            payload = hit.payload
            results.append(
                InlineQueryResultArticle(
                    title=f"💬 {payload['sender_name']} in {payload['chat_title']}",
                    description=payload['text'][:100],
                    input_message_content=InputTextMessageContent(
                        f"**Sender:** {payload['sender_name']}\n"
                        f"**Chat:** {payload['chat_title']}\n"
                        f"**Date:** {payload['date']}\n\n"
                        f"💬 *\"{payload['text']}\"*\n\n"
                        f"🔗 [Jump to message]({payload['link']})"
                    )
                )
            )

        await inline_query.answer(results, cache_time=1)

    except Exception as e:
        print(f"Error handling inline query: {e}")

if __name__ == "__main__":
    print("🤖 Telegram Inline Search Bot is running...")
    bot.run()
