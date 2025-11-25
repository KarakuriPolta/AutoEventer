import discord
import json
import logging
from google import genai
from google.genai.types import GenerateContentConfig, HarmCategory, HarmBlockThreshold, SafetySetting
from google.oauth2 import service_account
from datetime import datetime
import os
from pytz import timezone
import io
import db
import traceback

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('bot_errors.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# サービスアカウント認証情報の読み込み
SERVICE_ACCOUNT_FILE = 'google-credentials.json'
credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE,
    scopes=['https://www.googleapis.com/auth/cloud-platform']
)

# Vertex AIプロジェクト設定
with open(SERVICE_ACCOUNT_FILE, 'r') as f:
    service_account_info = json.load(f)
    VERTEX_PROJECT_ID = service_account_info.get('project_id')

VERTEX_PROJECT_REGION = os.getenv('VERTEX_PROJECT_REGION', 'us-central1')
TOKEN = os.getenv('DISCORD_TOKEN')

# Gemini APIクライアントの設定
safety_settings = [
    SafetySetting(
        category=HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        threshold=HarmBlockThreshold.BLOCK_NONE,
    ),
    SafetySetting(
        category=HarmCategory.HARM_CATEGORY_HARASSMENT,
        threshold=HarmBlockThreshold.BLOCK_NONE,
    ),
    SafetySetting(
        category=HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        threshold=HarmBlockThreshold.BLOCK_LOW_AND_ABOVE,
    ),
    SafetySetting(
        category=HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        threshold=HarmBlockThreshold.BLOCK_NONE,
    ),
]

# Vertex AI用のGenAIクライアント初期化
genai_client = genai.Client(
    vertexai=True,
    project=VERTEX_PROJECT_ID,
    location=VERTEX_PROJECT_REGION,
    credentials=credentials
)

client = discord.Client(intents=discord.Intents.all())

@client.event
async def on_ready():
    print('Bot is ready')
    try:
        db.init_db()
        logging.info("DB初期化完了")
    except Exception as e:
        logging.error(f"DB初期化失敗: {e}")
        # DB初期化に失敗してもbotは起動を続行

@client.event
async def on_message(message):
    # print(f'{message.channel}: {message.author}: {message.author.name}: {message.content}')
    if message.author == client.user:
        return
    if message.author.bot:
        return
    dm = (type(message.channel) == discord.DMChannel) and (client.user == message.channel.me)

    if dm or message.content.startswith('!ev'):
        # 以降はGeminiを用いるので、制限を確認
        user_id = str(message.author.id)
        try:
            allowed, error_message = db.check_limits(user_id)
            if not allowed:
                await message.channel.send(error_message)
                return
        except Exception as e:
            logging.error(f"DB制限チェックエラー: {e}")
            await message.channel.send("データベースエラーが発生しました。Bot管理者に連絡してください。")
            return
        # メッセージに画像が添付されている場合は初めの一枚を取得
        image = None
        if message.attachments != None:
            for attachment in message.attachments:
                # print("attachment")
                # print(attachment.content_type)
                if attachment.content_type.startswith('image/'):
                    image = await attachment.read()
                    break

        d = datetime.now()
        
        # システムプロンプト(固定の指示部分)
        system_instruction = [
            '# 役割',
            'あなたはイベント情報抽出の専門家です。与えられたメッセージからイベントの詳細を抽出し、JSON形式で出力します。',
            '',
            '# 出力形式',
            '出力はJSON文のみとし、1日ごとにイベントを区切り、"events"キーの配列に1つずつ"start_time"、"end_time"、"title"、"description"、"external"、"location"を含んだJSONオブジェクトを格納する形にしてください。',
            'イベントが1つだけでも要素1の配列にし、イベントが存在しない場合は空の配列にしてください。',
            'また、出力はjsonのプレーンテキストとし、コードブロックで囲んだりしないでください。',
            '',
            '# フィールドの詳細',
            '- start_time, end_time: "%Y-%m-%dT%H:%M:%SZ"形式のUTC時刻で記述',
            '- title: イベントのタイトル',
            '- description: 箇条書きで簡潔にまとめた説明文(配列ではなく改行コードを含めた文字列)',
            '- external: Discordボイスチャンネルの場合はfalse、それ以外はtrue',
            '- location: externalがfalseの場合はチャンネルURL、trueの場合は場所の名前やURL(不明なら「不明」)',
            '',
            '# 日時の扱い',
            '- プロンプトで与えられる日時は日本標準時(UTC+9)',
            '- end_timeが不明な場合はstart_timeから1時間後の日時を設定',
            '- 開催日時が明示的に過去である場合を除き、start_timeは現在時刻よりも後の日時を想定',
            '- start_time、end_timeが現在日時よりも過去の場合のみ、1年後など現在時刻よりも後の日時を設定',
            '- 同じ月でも現在日時よりあとの日付の場合は、今年のデータとする',
        ]
        
        # ユーザープロンプト(メッセージ内容)
        user_prompt = f"現在の日本標準時での日時は{d.strftime('%Y/%m/%d %H:%M:%S')}です。\n\n"
        
        if message.reference != None:
            reference = await message.channel.fetch_message(message.reference.message_id)
            user_prompt += f"返信先のメッセージ送信者:{reference.author.name}\n返信先のメッセージ:「{reference.content}」\n\n次がメッセージ本文です。返信先に対する指示がある場合、それに従ってください。\n\n"
            # 画像がまだ設定されておらず返信先のメッセージに画像が添付されている場合は初めの一枚を取得
            if image == None and reference.attachments:
                for attachment in reference.attachments:
                    if attachment.content_type and attachment.content_type.startswith('image/'):
                        image = await attachment.read()
                        break
        
        user_prompt += f"メッセージの送信者:{message.author.name}\n"
        user_prompt += f"イベントについて記述したメッセージ:「{message.content.replace('!ev','').strip()}」"
        
        # Gemini APIリクエスト
        try:
            response_obj = genai_client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=user_prompt,
                config=GenerateContentConfig(
                    system_instruction=system_instruction,
                    safety_settings=safety_settings
                ),
            )
            
            if response_obj.text is None:
                logging.error(f"AI応答がNone: user={message.author.name}, message={message.content[:100]}")
                await message.channel.send("AIからの応答が取得できませんでした。")
                return
            
            response = str.strip(response_obj.text)
            logging.info(f"AI応答取得成功: user={message.author.name}, response_length={len(response)}")
        
        except Exception as e:
            logging.error(f"Gemini APIエラー: user={message.author.name}, error={str(e)}\n{traceback.format_exc()}")
            await message.channel.send("AIとの通信中にエラーが発生しました。しばらく時間をおいて再度お試しください。")
            return

        # responseを解釈して、日付、タイトル、説明文を取り出す
        if response.startswith("```"):
            response = str.strip(response[3:-3])
        if response.startswith("json"):
            response = str.strip(response[4:])

        try:
            parsed = json.loads(response)
            logging.info(f"JSONパース成功: events_count={len(parsed.get('events', []))}")
        except json.JSONDecodeError as e:
            logging.error(f"JSONパースエラー: user={message.author.name}, error={str(e)}, response={response[:500]}")
            await message.channel.send("AIの応答形式が不正です。もう一度お試しください。")
            return

        # イベントがないまたはサイズ0の場合は警告を出す
        if 'events' not in parsed or len(parsed['events']) == 0:
            await message.channel.send("イベントが見つかりませんでした。")
            return

        responseMessage = "以下のイベントを登録しました。\n"
        ical_text = ""

        try:
            # イベントを1つずつ取り出してdiscordのイベントとして登録
            for event in parsed['events']:
                # UTCでの日時
                start_time = datetime.strptime(event['start_time'], "%Y-%m-%dT%H:%M:%S%z")
                end_time = datetime.strptime(event['end_time'], "%Y-%m-%dT%H:%M:%S%z")
                title = event['title']
                description = event['description']
                external = event['external']

                # channelの存在確認
                channel = None
                if not external:
                    try:
                        # idを抽出
                        _loc = str.strip(event['location'])
                        if _loc[-1]=="/":
                            _loc = _loc[:-1]
                        _loc = _loc.split('/')[-1]
                        channel = message.guild.get_channel(int(_loc))
                    except (ValueError, TypeError):
                        channel = None

                if channel is None:
                    external = True

                if external:
                    entity_type = discord.EntityType.external
                    location = event['location']  # 任意の場所
                    channel = None
                    if not dm: # DMの場合はイベントを作成出来ないので登録を無視
                        if image != None:
                            await message.guild.create_scheduled_event(name=title, description=description, start_time=start_time, end_time=end_time, entity_type=entity_type, location=location, privacy_level=discord.PrivacyLevel.guild_only, image=image)
                        else:
                            await message.guild.create_scheduled_event(name=title, description=description, start_time=start_time, end_time=end_time, entity_type=entity_type, location=location, privacy_level=discord.PrivacyLevel.guild_only)
                else:
                    entity_type = discord.EntityType.voice
                    location = None
                    if not dm: # DMの場合はイベントを作成出来ないので登録を無視
                        if image != None:
                            await message.guild.create_scheduled_event(name=title, description=description, start_time=start_time, end_time=end_time, entity_type=entity_type, channel=channel, privacy_level=discord.PrivacyLevel.guild_only, image=image)
                        else:
                            await message.guild.create_scheduled_event(name=title, description=description, start_time=start_time, end_time=end_time, entity_type=entity_type, channel=channel, privacy_level=discord.PrivacyLevel.guild_only)

                # icalendar形式で出力
                ical_text += "BEGIN:VEVENT\n"
                ical_text += f"SUMMARY:{title}\n"
                description_replaced = description.replace('\r', '').replace('\n', '\\n')
                ical_text += f"DESCRIPTION:{description_replaced}\n"
                ical_text += f"DTSTART:{start_time.strftime('%Y%m%dT%H%M%SZ')}\n"
                ical_text += f"DTEND:{end_time.strftime('%Y%m%dT%H%M%SZ')}\n"
                if external:
                    ical_text += f"LOCATION:{location}\n"
                else:
                    ical_text += f"LOCATION:Discord Voice Channel\n"
                ical_text += "END:VEVENT\n"

        except Exception as e:
            logging.error(
                f"イベント作成エラー: user={message.author.name}, "
                f"error={str(e)}, parsed_data={json.dumps(parsed, ensure_ascii=False)[:500]}\n"
                f"{traceback.format_exc()}"
            )
            await message.channel.send("イベントの作成中にエラーが発生しました。Botの管理者に連絡してください。")
            return

        if dm: # DMの場合はイベントを作成出来ないので登録を無視
            responseMessage = "以下の内容のスケジュールファイルを作成しました。\n"
        else:
            responseMessage = "以下のイベントを登録しました。\n"
        for event in parsed['events']:
            start_time = datetime.strptime(event['start_time'], "%Y-%m-%dT%H:%M:%S%z")
            end_time = datetime.strptime(event['end_time'], "%Y-%m-%dT%H:%M:%S%z")
            # 日本時間に変換してログに追加
            responseMessage += f"```タイトル：{event['title']}\n説明：{event['description']}\n開始（日本時間）：{start_time.astimezone(timezone('Asia/Tokyo')).strftime('%Y/%m/%d %H:%M')}\n終了（日本時間）：{end_time.astimezone(timezone('Asia/Tokyo')).strftime('%Y/%m/%d %H:%M')}\n場所：{event['location']}```\n\n"

        # ical_textをメモリ上のファイルに一時保存してアップロード
        with io.BytesIO() as f:
            f.write("BEGIN:VCALENDAR\nVERSION:2.0\n".encode('utf-8'))
            f.write(ical_text.encode('utf-8'))
            f.write("END:VCALENDAR\n".encode('utf-8'))
            f.seek(0)  # ファイルポインタを先頭に戻す
            await message.channel.send(responseMessage, file=discord.File(fp=f, filename="event.ics"))

if TOKEN is None:
    raise ValueError("DISCORD_TOKEN環境変数が設定されていません")

client.run(TOKEN)