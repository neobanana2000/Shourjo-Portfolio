# ============================================================
#  Rebuilds the two performance-page charts from live prices.
#  Run by .github/workflows/charts.yml after each market close.
#
#  Reads  : portfolio.csv  (ticker, buy price, shares, fallback price)
#  Fetches: current price for each ticker from Finnhub
#  Writes : contribution.png, allocation.png
# ============================================================

library(ggplot2)
library(jsonlite)

p   <- read.csv("portfolio.csv")
key <- Sys.getenv("FINNHUB_KEY")

# ---- current price for one ticker, or NA if Finnhub has nothing ----
get_price <- function(ticker) {
  if (key == "") return(NA)
  url <- paste0("https://finnhub.io/api/v1/quote?symbol=", ticker, "&token=", key)
  q <- try(fromJSON(url), silent = TRUE)
  if (inherits(q, "try-error") || is.null(q$c) || q$c <= 0) return(NA)
  q$c
}

# ---- fetch all 27, pausing so we stay inside the 60/minute free tier ----
p$price <- NA
for (i in seq_len(nrow(p))) {
  p$price[i] <- get_price(p$ticker[i])
  Sys.sleep(0.3)
}

# A ticker Finnhub cannot price keeps its last known close, so one dead
# symbol never blanks the chart or distorts the totals.
live <- sum(!is.na(p$price))
p$price[is.na(p$price)] <- p$fallback_price[is.na(p$price)]
cat("priced", live, "of", nrow(p), "from Finnhub\n")

p$value <- p$shares * p$price
p$gain  <- p$value - p$shares * p$buy_price
p$share <- p$value / sum(p$value) * 100

stamp <- format(Sys.Date(), "%d %b %Y")

# ---- chart 1: gain per holding ----
ggplot(p, aes(reorder(ticker, gain), gain, fill = gain > 0)) +
  geom_col() + coord_flip() + theme_minimal() +
  labs(x = NULL, y = "Gain ($)", caption = paste("live prices", stamp)) +
  theme(legend.position = "none")

ggsave("contribution.png", width = 7, height = 7, dpi = 110)

# ---- chart 2: share of portfolio per holding ----
ggplot(p, aes(reorder(ticker, share), share)) +
  geom_col(fill = "#2a78d6") + coord_flip() + theme_minimal() +
  labs(x = NULL, y = "Share of portfolio (%)", caption = paste("live prices", stamp))

ggsave("allocation.png", width = 7, height = 7, dpi = 110)

cat("portfolio value", round(sum(p$value), 2),
    "| total gain", round(sum(p$gain), 2), "\n")
