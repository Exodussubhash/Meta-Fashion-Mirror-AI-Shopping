import './globals.css';
export const metadata = {
  title: 'FashionMirror — Your style. Reflected.',
  description: 'Discover fashion from independent brands, with a personal AI stylist.'
};
export default function Layout({
  children
}: {
  children: React.ReactNode;
}) {
  return <html lang="en">
    <body>
      {children}
    </body>
  </html>;
}
