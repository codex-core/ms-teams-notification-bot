#!/usr/bin/env bash
#
# send-notification.sh
# ---------------------
# Test harness for the ms-teams-notification-bot. It builds a notification from
# the canonical JSON schema and triggers/tests BOTH delivery scenarios from a
# single command:
#
#   1. Teams Incoming Webhook  -> POST an Adaptive Card to a webhook URL.
#   2. Power Automate Flow Bot -> POST the raw notification JSON to an HTTP
#                                 trigger URL.
#
# It can also publish the notification JSON to the SNS topic so the real
# Lambdas are exercised end-to-end.
#
# Canonical notification schema (built from flags or read with --file/--stdin):
#
#   {
#     "recipients": ["alice@example.com", "bob@example.com"],
#     "dateTime":   ["2026-08-17T17:06:39Z"],
#     "message":    "Something happened",
#     "links":      [{ "title": "Runbook", "url": "https://example.com" }],  // optional
#     "type":       "info"                                                    // optional: info|warning|error
#   }
#
# `type` controls the Adaptive Card colour used for the webhook scenario:
#   info    -> accent    (blue)
#   warning -> warning   (amber)
#   error   -> attention (red)
#
# Requirements: bash, jq, curl (and the AWS CLI when using --sns).
#
set -euo pipefail

PROG="$(basename "$0")"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
die() { echo "$PROG: error: $*" >&2; exit 1; }

usage() {
  cat <<EOF
Usage: $PROG [notification fields] [targets] [options]

Notification fields (used when NOT reading from --file/--stdin):
  -m, --message TEXT        Notification message body (required)
  -r, --recipient EMAIL     Recipient / @mention email (repeatable)
  -d, --datetime ISO8601    Date-time value (repeatable; default: current UTC time)
  -l, --link LINK           Link as "Title=https://url" or bare "https://url"
                            (repeatable, optional)
  -t, --type TYPE           info | warning | error (default: info)
      --title TEXT          Optional card title (default: "<TYPE> Notification")

Input (alternative to the fields above):
  -f, --file PATH           Read the notification JSON (schema above) from a file
      --stdin               Read the notification JSON from standard input

Targets (any combination; falls back to the matching env var when value omitted):
  -w, --webhook [URL]       Teams Incoming Webhook URL   (env: WEBHOOK_URL)
  -F, --flow [URL]          Power Automate HTTP trigger  (env: FLOW_TRIGGER_URL)
  -s, --sns [TOPIC_ARN]     SNS topic ARN                (env: SNS_TOPIC_ARN)
      --all                 Use WEBHOOK_URL, FLOW_TRIGGER_URL and SNS_TOPIC_ARN
                            from the environment.

Options:
  -n, --dry-run             Print the payloads without sending anything
  -h, --help                Show this help and exit

Examples:
  # Dry-run: preview both payloads
  $PROG -m "Deploy finished" -r alice@example.com -t info --dry-run

  # Test both HTTP scenarios directly
  $PROG -m "Disk almost full" -r ops@example.com -t warning \\
        -l "Runbook=https://wiki/df" \\
        --webhook https://outlook.office.com/webhook/... \\
        --flow    https://prod-xx.logic.azure.com/...

  # Read a prepared notification file and publish to SNS end-to-end
  $PROG --file scripts/examples/notification.json --sns arn:aws:sns:...:topic
EOF
}

need() { command -v "$1" >/dev/null 2>&1 || die "'$1' is required but not installed"; }

# Convert the remaining positional args into a JSON array of strings.
to_json_array() {
  if [ "$#" -eq 0 ]; then
    echo '[]'
    return
  fi
  printf '%s\0' "$@" | jq -Rs 'split("\u0000") | map(select(length > 0))'
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
need jq
need curl

message=""
title=""
type="info"
recipients=()
datetimes=()
links=()

input_file=""
read_stdin=false

webhook_url="${WEBHOOK_URL:-}"
flow_url="${FLOW_TRIGGER_URL:-}"
sns_arn="${SNS_TOPIC_ARN:-}"
want_webhook=false
want_flow=false
want_sns=false
dry_run=false

# Does the next argument look like a value (not another flag / not absent)?
is_value() { [ "$#" -gt 0 ] && [ "${1#-}" = "$1" ]; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    -m|--message)    shift; [ "$#" -gt 0 ] || die "--message needs a value"; message="$1" ;;
    --title)         shift; [ "$#" -gt 0 ] || die "--title needs a value"; title="$1" ;;
    -t|--type)       shift; [ "$#" -gt 0 ] || die "--type needs a value"; type="$1" ;;
    -r|--recipient)  shift; [ "$#" -gt 0 ] || die "--recipient needs a value"; recipients+=("$1") ;;
    -d|--datetime)   shift; [ "$#" -gt 0 ] || die "--datetime needs a value"; datetimes+=("$1") ;;
    -l|--link)       shift; [ "$#" -gt 0 ] || die "--link needs a value"; links+=("$1") ;;
    -f|--file)       shift; [ "$#" -gt 0 ] || die "--file needs a value"; input_file="$1" ;;
    --stdin)         read_stdin=true ;;
    -w|--webhook)    want_webhook=true; if is_value "${2:-}"; then shift; webhook_url="$1"; fi ;;
    -F|--flow)       want_flow=true;    if is_value "${2:-}"; then shift; flow_url="$1"; fi ;;
    -s|--sns)        want_sns=true;     if is_value "${2:-}"; then shift; sns_arn="$1"; fi ;;
    --all)           want_webhook=true; want_flow=true; want_sns=true ;;
    -n|--dry-run)    dry_run=true ;;
    -h|--help)       usage; exit 0 ;;
    --)              shift; break ;;
    -*)              die "unknown option: $1 (use --help)" ;;
    *)               die "unexpected argument: $1 (use --help)" ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# Build / normalise the notification JSON
# ---------------------------------------------------------------------------
# shellcheck disable=SC2016  # jq programs are intentionally single-quoted
normalise='{
  recipients: (.recipients // []),
  dateTime:   (.dateTime // []),
  message:    (.message // ""),
  links:      ((.links // []) | map(
                 if type == "string" then { title: ., url: . }
                 else { title: (.title // .name // .url // .href // ""),
                        url:   (.url // .href // "") }
                 end)),
  type:       (.type // "info")
} + (if (.title // "") != "" then { title: .title } else {} end)'

if [ -n "$input_file" ] || [ "$read_stdin" = true ]; then
  if [ -n "$input_file" ] && [ "$read_stdin" = true ]; then
    die "use either --file or --stdin, not both"
  fi
  if [ -n "$input_file" ]; then
    [ -f "$input_file" ] || die "file not found: $input_file"
    raw="$(cat "$input_file")"
  else
    raw="$(cat)"
  fi
  echo "$raw" | jq empty 2>/dev/null || die "input is not valid JSON"
  notification="$(echo "$raw" | jq "$normalise")"
else
  [ -n "$message" ] || die "--message is required (or use --file/--stdin)"

  recipients_json="$(to_json_array "${recipients[@]+"${recipients[@]}"}")"
  datetime_json="$(to_json_array "${datetimes[@]+"${datetimes[@]}"}")"
  if [ "$(echo "$datetime_json" | jq 'length')" -eq 0 ]; then
    datetime_json="$(jq -n --arg now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '[$now]')"
  fi

  links_json='[]'
  for l in "${links[@]+"${links[@]}"}"; do
    if [[ "$l" == *"="* ]]; then
      lt="${l%%=*}"; lu="${l#*=}"
    else
      lt="$l"; lu="$l"
    fi
    links_json="$(jq --arg t "$lt" --arg u "$lu" '. + [{ title: $t, url: $u }]' <<<"$links_json")"
  done

  notification="$(jq -n \
    --argjson recipients "$recipients_json" \
    --argjson dateTime "$datetime_json" \
    --arg message "$message" \
    --argjson links "$links_json" \
    --arg type "$type" \
    --arg title "$title" \
    '{ recipients: $recipients, dateTime: $dateTime, message: $message, links: $links, type: $type }
     + (if $title != "" then { title: $title } else {} end)')"
fi

# Validate the notification.
[ "$(echo "$notification" | jq -r '.message | length')" -gt 0 ] || die "notification message must not be empty"
ntype="$(echo "$notification" | jq -r '.type')"
case "$ntype" in
  info|warning|error) ;;
  *) die "type must be one of: info, warning, error (got '$ntype')" ;;
esac

# ---------------------------------------------------------------------------
# Build the Adaptive Card (webhook scenario) from the notification JSON
# ---------------------------------------------------------------------------
# shellcheck disable=SC2016  # jq programs are intentionally single-quoted
build_card='
  (.type // "info")                                             as $type |
  ({ "info": "accent",   "warning": "warning",  "error": "attention" }[$type] // "accent")   as $color |
  ({ "info": "accent",   "warning": "warning",  "error": "attention" }[$type] // "accent")   as $style |
  (.title // (($type | ascii_upcase) + " Notification"))        as $title |
  (.recipients // [])                                           as $recips |
  (.dateTime // [])                                             as $dts |
  (.links // [])                                                as $links |
  ($recips | map(. as $e | ($e | split("@")[0]) as $d
                 | { type: "mention", text: ("<at>" + $d + "</at>"),
                     mentioned: { id: $e, name: $d } }))        as $entities |
  ($recips | map("<at>" + (split("@")[0]) + "</at>") | join(" ")) as $tags |
  (if ($tags | length) > 0 then (.message + "\n\n" + $tags) else .message end) as $body |
  ($links | map({ type: "Action.OpenUrl",
                  title: (.title // .url // "Open"),
                  url: (.url // .title // "") })
          | map(select(.url != "")))                           as $actions |
  {
    type: "message",
    attachments: [{
      contentType: "application/vnd.microsoft.card.adaptive",
      content: ({
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        type: "AdaptiveCard",
        version: "1.4",
        body: ([
          { type: "Container", style: $style, bleed: true, items: [
            { type: "TextBlock", size: "Large", weight: "Bolder",
              color: $color, text: $title, wrap: true }
          ]},
          { type: "TextBlock", text: $body, wrap: true }
        ] + (if ($dts | length) > 0
             then [{ type: "TextBlock", spacing: "Small", isSubtle: true,
                     size: "Small", wrap: true,
                     text: ("🕒 " + ($dts | join(", "))) }]
             else [] end)),
        msteams: { entities: $entities }
      } + (if ($actions | length) > 0 then { actions: $actions } else {} end))
    }]
  }'

card="$(echo "$notification" | jq "$build_card")"

# ---------------------------------------------------------------------------
# Deliver
# ---------------------------------------------------------------------------
post_json() {
  local url="$1" body="$2" label="$3" tmp code
  tmp="$(mktemp)"
  if code="$(curl -sS -o "$tmp" -w '%{http_code}' \
              -X POST -H 'Content-Type: application/json' \
              --data-binary "$body" "$url")"; then
    echo "[$label] HTTP $code"
    if [ "${code:0:1}" != "2" ]; then
      echo "[$label] response: $(cat "$tmp")" >&2
    fi
  else
    echo "[$label] request failed" >&2
    rm -f "$tmp"
    return 1
  fi
  rm -f "$tmp"
}

any_target=false
[ "$want_webhook" = true ] && any_target=true
[ "$want_flow" = true ] && any_target=true
[ "$want_sns" = true ] && any_target=true

if [ "$dry_run" = true ] || [ "$any_target" = false ]; then
  [ "$any_target" = false ] && [ "$dry_run" = false ] && \
    echo "No target specified; showing a dry run. Use --webhook/--flow/--sns to send." >&2
  echo "=== Notification JSON (flow / SNS payload) ==="
  echo "$notification" | jq .
  echo
  echo "=== Adaptive Card (webhook payload) ==="
  echo "$card" | jq .
  exit 0
fi

rc=0

if [ "$want_webhook" = true ]; then
  [ -n "$webhook_url" ] || die "webhook target selected but no URL given (flag or WEBHOOK_URL)"
  post_json "$webhook_url" "$card" "webhook" || rc=1
fi

if [ "$want_flow" = true ]; then
  [ -n "$flow_url" ] || die "flow target selected but no URL given (flag or FLOW_TRIGGER_URL)"
  post_json "$flow_url" "$notification" "flow" || rc=1
fi

if [ "$want_sns" = true ]; then
  [ -n "$sns_arn" ] || die "sns target selected but no topic ARN given (flag or SNS_TOPIC_ARN)"
  need aws
  if aws sns publish --topic-arn "$sns_arn" --message "$notification" >/dev/null; then
    echo "[sns] published to $sns_arn"
  else
    echo "[sns] publish failed" >&2
    rc=1
  fi
fi

exit "$rc"
