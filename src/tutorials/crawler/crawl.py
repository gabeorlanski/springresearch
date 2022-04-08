import os

from scrapy.crawler import CrawlerProcess
from scrapy.utils.project import get_project_settings
from pathlib import Path
import click

if Path().stem == 'crawler':
    root = Path().absolute().parents[2]
else:
    root = Path().absolute()
save_path = root.joinpath('data/crawled_tutorials')
print(save_path)


@click.command()
@click.argument('name')
@click.argument('start_url')
@click.option('--allowed-path', default=None)
def crawl(name, start_url, allowed_path):
    os.environ.setdefault('SCRAPY_SETTINGS_MODULE', 'crawler.settings')
    settings = get_project_settings()
    if not root.joinpath('data/crawled_maps').exists():
        root.joinpath('data/crawled_maps').mkdir()

    with root.joinpath(f'data/crawled_maps/{name}.json').open('w'):
        pass
    settings['FEEDS'] = {
        str(root.joinpath(f'data/crawled_maps/{name}.json')): {"format": "json"}
    }
    process = CrawlerProcess(settings)

    if name == "lxml":
        disallow = [r'.*/[0-9]+\.[0-9]+\/.*', r'.*/changes.*', '.*#.*','.*/files/.*']
    else:
        disallow = []

    # 'followall' is the name of one of the spiders of the project.
    process.crawl(
        'docspider',
        out_name=name,
        url=start_url,
        output_path=save_path,
        allowed_path=allowed_path,
        disallow=disallow
    )
    process.start()


if __name__ == '__main__':
    crawl()
