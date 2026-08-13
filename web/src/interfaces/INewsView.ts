// Types for NewsView.tsx (the News tab).

// One headline from GET /api/news (ib_price_server.py's _news_snapshot).
export interface Article {
  articleId: string
  time: string
  provider: string
  headline: string
  sentiment: number | null
}

// The raw {ticker: [article, ...]} payload GET /api/news returns.
export type ArticlesByTicker = Record<string, Article[]>

// One flattened, globally-sorted row -- an Article plus which ticker it's
// about (see flattenNews).
export interface FlatArticle extends Article {
  ticker: string
}

// The live EventSource positions payload -- shares only, same minimal
// shape RecommendationsView.tsx's own Position uses (this page only needs
// it to know which tickers are held, for the "held only" filter).
export interface Position {
  shares?: number
}
export type PositionsByTicker = Record<string, Position>
